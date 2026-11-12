"""Durable field-level category application and confirmed compensating rollback."""
from copy import deepcopy
from dataclasses import replace
import json
import time
from datetime import datetime, timezone

from finance_bot.category_migration import cell_address, read_google_spreadsheet
from finance_bot.category_projections import sheet_rows
from finance_bot.category_schema import AccessMode, METADATA_SHEET, WorkbookMetadata
from finance_bot.migration_apply import MigrationError, _private_json, fingerprint
from finance_bot.native_migration import _quote
from finance_bot.dashboard_changes import digest, validate_presentation
from finance_bot.category_journal import (
    JOURNAL_VERSION, CategoryConflict, cell_steps, cell_state, check_edits,
    financial_invariants, require_business, rollback_projection,
)
from finance_bot.category_native_views import (
    validation_steps, presentation_step, view_state, view_expected,
    capacity_steps, read_capacity, capacity_matches,
)


def _http_status(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status=getattr(getattr(error,'response',None),'status_code',None)
        if isinstance(status,int):return status
        error=error.__cause__ or error.__context__
    return None


def _transient(error):
    # Only exception types/status codes; never inspect bodies or cell values.
    from requests.exceptions import Timeout, ConnectionError
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status=getattr(getattr(error,'response',None),'status_code',None)
        if status is not None:return status in {408,429,500,502,503,504}
        if isinstance(error,(Timeout,ConnectionError)):return True
        error=error.__cause__ or error.__context__
    return False


def safe_failure(error, stage, attempts=0):
    if isinstance(error, CategoryConflict):
        return {'stage':error.stage,'code':error.code,'sheet':error.sheet,
                'cell':cell_address(*error.key) if error.key else '', 'attempts':attempts}
    status=_http_status(error)
    code=('google_quota' if status==429 else 'google_request_invalid' if status==400 else
          'temporary_google' if _transient(error) else 'google_access' if status in {401,403,404} else 'operation_failed')
    return {'stage':stage,'code':code,'sheet':'','cell':'','attempts':attempts,
            'http_status':status if isinstance(status,int) else None,'exception_type':type(error).__name__}


def diagnostic(error, request_id=""):
    prefix = f"Запрос: {request_id}. " if request_id else ""
    return prefix + (f"Этап: {error.get('stage','unknown')}. Код: {error.get('code','operation_failed')}. "
            f"Область: {error.get('sheet','')}!{error.get('cell','')}. Попыток: {error.get('attempts',0)}. "
            f"HTTP: {error.get('http_status') or '—'}. Тип: {error.get('exception_type','—')}.")


def _signature(state):
    def clean(step):return {k:v for k,v in step.items() if k not in {'phase','attempts'}}
    return digest({'request_id':state['request_id'],'spreadsheet_id':state['spreadsheet_id'],
                   'steps':[clean(s) for s in state['steps']], 'pending':clean(state['pending'])})


def _read_edits(target, step):
    edits=step['edits'];name=step['sheet']
    if callable(getattr(type(target),'values_batch_get',None)):
        lo_r=min(e['key'][0] for e in edits);hi_r=max(e['key'][0] for e in edits)
        lo_c=min(e['key'][1] for e in edits);hi_c=max(e['key'][1] for e in edits)
        area=_quote(name)+cell_address(lo_r,lo_c)+':'+cell_address(hi_r,hi_c)
        values=target.values_batch_get([area],params={'valueRenderOption':'FORMULA'})['valueRanges'][0].get('values',[])
        result=[]
        for edit in edits:
            r,c=edit['key'][0]-lo_r,edit['key'][1]-lo_c
            value=values[r][c] if r<len(values) and c<len(values[r]) else ''
            result.append({'formula':value} if isinstance(value,str) and value.startswith('=') else {'value':value})
        return result
    book=read_google_spreadsheet(target)
    if name not in book.sheets:raise CategoryConflict('readback',name,code='missing_sheet')
    return [cell_state(book.sheets[name],tuple(edit['key']),target) for edit in edits]


def _run_step(target, step, state, path, *, inverse=False):
    side,other=('before','after') if inverse else ('after','before')
    stage=('rollback:' if inverse else 'apply:')+step['id']
    state['stage']=stage
    # Three attempts per invocation, each preceded by a fresh readback. Attempts
    # persisted across invocations are diagnostic, not an infinite auto-retry.
    for attempt in range(3):
        step['attempts']=step.get('attempts',0)+1
        try:
            if step['kind']=='capacity':
                actual=read_capacity(target,step)
                if capacity_matches(actual,step[side]):
                    step['phase']='reverted' if inverse else 'verified';_private_json(path,state);return
                if not capacity_matches(actual,step[other]):
                    raise CategoryConflict(stage,step['sheet'],code='grid_shrunk')
            elif step['kind']=='cells':
                actual=_read_edits(target,step);pending=[]
                for edit,observed in zip(step['edits'],actual):
                    if observed==edit[side]:continue
                    if observed!=edit[other]:raise CategoryConflict(stage,step['sheet'],tuple(edit['key']))
                    pending.append(edit)
                if not pending:
                    step['phase']='reverted' if inverse else 'verified';_private_json(path,state);return
            else:
                actual=view_state(target,step)
                if actual==view_expected(step,side):
                    step['phase']='reverted' if inverse else 'verified';_private_json(path,state);return
                if actual!=view_expected(step,other):raise CategoryConflict(stage,code='native_view_conflict')
            step['phase']='reverting' if inverse else 'writing'
            _private_json(path,state)  # Must succeed before remote mutation.
            if step['kind']=='capacity':
                requests=[]
                for key,dimension in (('rows','ROWS'),('columns','COLUMNS')):
                    if step[side][key]>actual[key]:
                        requests.append({'appendDimension':{'sheetId':step['sheet_id'],'dimension':dimension,'length':step[side][key]-actual[key]}})
                if requests:target.batch_update({'requests':requests})
            elif step['kind']=='cells':
                for option in ('RAW','USER_ENTERED'):
                    data=[]
                    for edit in pending:
                        value=edit[side]
                        if ('formula' in value)!=(option=='USER_ENTERED'):continue
                        data.append({'range':_quote(step['sheet'])+cell_address(*edit['key']),
                                     'values':[[value.get('formula',value.get('value',''))]]})
                    if data:target.values_batch_update({'valueInputOption':option,'data':data})
            else:
                requests=step['inverse' if inverse else 'forward']
                if requests:target.batch_update({'requests':requests})
            # A successful transport response alone is not a verified result.
            if step['kind']=='capacity':
                if not capacity_matches(read_capacity(target,step),step[side]):
                    raise CategoryConflict(stage,step['sheet'],code='grid_readback_mismatch')
            elif step['kind']=='cells':
                observed=_read_edits(target,step)
                for edit,value in zip(step['edits'],observed):
                    if value!=edit[side]:raise CategoryConflict(stage,step['sheet'],tuple(edit['key']),'readback_mismatch')
            elif view_state(target,step)!=view_expected(step,side):
                raise CategoryConflict(stage,code='native_readback_mismatch')
            step['phase']='reverted' if inverse else 'verified';_private_json(path,state);return
        except Exception as error:
            state['last_error']=safe_failure(error,stage,step.get('attempts',0))
            if not state['last_error']['sheet']:
                state['last_error']['sheet']=step.get('sheet','Аналитика' if step['kind']=='presentation' else 'Траты')
            state.setdefault('errors',[]).append({'time':datetime.now(timezone.utc).isoformat(),**state['last_error']})
            _private_json(path,state)
            if not _transient(error) or attempt==2:raise
            time.sleep(.25*(2**attempt))


def _meta_step(source, after, name):
    edits=[]
    original=source.sheets[METADATA_SHEET]
    values={'access_mode':after.access_mode.value,'pending_request_id':after.pending_request_id,
            'taxonomy_revision':after.taxonomy_revision}
    for index,row in enumerate(sheet_rows(original),1):
        if row[0] in values:
            edits.append({'key':[index,2],'before':{'value':row[1]},'after':{'value':values[row[0]]}})
    return {'id':name,'kind':'cells','sheet':METADATA_SHEET,'edits':edits,'phase':'prepared'}


def _meta_from(book):return WorkbookMetadata.from_rows(sheet_rows(book.sheets[METADATA_SHEET]))


def _ready_metadata(target):
    if callable(getattr(type(target),'values_get',None)):
        result=target.values_get("'_financebot_meta'",params={'valueRenderOption':'UNFORMATTED_VALUE'})
        rows=[list(row)+['']*(2-len(row)) for row in result.get('values',[])]
        return WorkbookMetadata.from_rows(rows)
    return _meta_from(read_google_spreadsheet(target))


def _validate_identity(source,change,target):
    if target.id!=change.spreadsheet_id or fingerprint(source)!=change.source_digest or fingerprint(change.output)!=change.target_digest:
        raise CategoryConflict('plan',code='plan_modified')


def _load_or_create(source,change,target,directory,*,current=None):
    path=directory/(change.request_id+'-native.json')
    if path.exists():
        state=json.loads(path.read_text())
        if state.get('source_digest')!=change.source_digest or state.get('target_digest')!=change.target_digest:
            raise CategoryConflict('journal',code='plan_modified')
        if state.get('version')!=JOURNAL_VERSION:
            if state.get('finished'):
                meta=_meta_from(current if current is not None else read_google_spreadsheet(target));meta.require_ready(target.id,expected_revision=change.expected_revision+1)
                return path,state
            raise CategoryConflict('journal',code='legacy_pending_requires_operator')
        if (state.get('request_id')!=change.request_id or state.get('spreadsheet_id')!=target.id
                or state.get('plan_signature')!=_signature(state) or state.get('presentation_digest')!=change.presentation_digest):
            raise CategoryConflict('journal',code='plan_modified')
        return path,state
    current=current if current is not None else read_google_spreadsheet(target)
    if fingerprint(current)!=change.source_digest:raise CategoryConflict('preview',code='stale_source')
    meta=_meta_from(current);meta.require_ready(target.id,expected_revision=change.expected_revision)
    validate_presentation(target,change.presentation,change.presentation_digest)
    pending=replace(meta,access_mode=AccessMode.MAINTENANCE,pending_request_id=change.request_id)
    steps=cell_steps(source,change.output,target)
    steps=capacity_steps(target,steps)+steps
    steps += [presentation_step(target,change.presentation),validation_steps(target,change.output)]
    state={'version':JOURNAL_VERSION,'request_id':change.request_id,'spreadsheet_id':target.id,'source_digest':change.source_digest,'target_digest':change.target_digest,
           'presentation_digest':change.presentation_digest,'phase':'applying','finished':False,
           'steps':steps,'pending':_meta_step(source,pending,'maintenance'),'last_error':None}
    state['plan_signature']=_signature(state)
    _private_json(path,state)
    return path,state


def _require_owner(meta,change,*,allow_ready=False,rollback_revision=None):
    if meta.spreadsheet_id!=change.spreadsheet_id:raise CategoryConflict('metadata',code='scope')
    pending=meta.pending_request_id==change.request_id and meta.access_mode==AccessMode.MAINTENANCE and meta.taxonomy_revision in {change.expected_revision,change.expected_revision+1}
    initial=not meta.pending_request_id and meta.access_mode==AccessMode.OPEN and meta.taxonomy_revision==change.expected_revision
    final=allow_ready and not meta.pending_request_id and meta.access_mode==AccessMode.OPEN and meta.taxonomy_revision in {change.expected_revision+1,rollback_revision}
    if not (pending or initial or final):raise CategoryConflict('metadata',code='request_changed')


def apply_change_to_google_copy(source, change, target, directory, *, confirmed, maintenance_confirmed):
    if confirmed is not True or maintenance_confirmed is not True:raise MigrationError('Confirm the exact category plan and suspend manual writes.')
    _validate_identity(source,change,target)
    existed=(directory/(change.request_id+'-native.json')).exists()
    current=read_google_spreadsheet(target)
    path,state=_load_or_create(source,change,target,directory,current=current)
    if state.get('finished'):
        if state.get('phase')=='rolled_back':raise CategoryConflict('apply',code='already_rolled_back')
        _ready_metadata(target).require_ready(target.id,expected_revision=change.expected_revision+1)
        return
    if state['phase'].startswith('rollback') or state['phase']=='rolling_back':raise CategoryConflict('apply',code='rollback_in_progress')
    try:
        meta=_meta_from(current)
        _require_owner(meta,change,allow_ready=state['phase']=='finalizing')
        financial_invariants(source,current)
        check_edits(current,state['steps'],target,'preflight')
        if existed:validate_presentation(target,change.presentation,change.presentation_digest,resume=True)
        if state['phase']!='finalizing':_run_step(target,state['pending'],state,path)
        for step in state['steps']:_run_step(target,step,state,path)
        state['stage']='verify';_private_json(path,state)
        final=read_google_spreadsheet(target)
        financial_invariants(source,final);require_business(final,target.id)
        check_edits(final,state['steps'],target,'verify')
        # Every written field must be the target, not merely before-or-after.
        for step in state['steps']:
            if step['kind']=='cells':
                for edit in step['edits']:
                    if cell_state(final.sheets[step['sheet']],tuple(edit['key']),target)!=edit['after']:
                        raise CategoryConflict('verify',step['sheet'],tuple(edit['key']))
        pending=_meta_from(source)
        state['phase']='finalizing'
        if 'publish' not in state:
            # Final meta before-image is maintenance, not the original open state.
            publish=_meta_step(source,replace(pending,taxonomy_revision=change.expected_revision+1),'publish')
            for edit,p in zip(publish['edits'],state['pending']['edits']):edit['before']=p['after']
            state['publish']=publish
        _private_json(path,state);_run_step(target,state['publish'],state,path)
        _ready_metadata(target).require_ready(target.id,expected_revision=change.expected_revision+1)
        state.update(phase='complete',finished=True,last_error=None);_private_json(path,state)
    except Exception as error:
        if not state.get('last_error') or isinstance(error,CategoryConflict):
            state['last_error']=safe_failure(error,state.get('stage','preflight'),(state.get('last_error') or {}).get('attempts',0))
        _private_json(path,state);raise


def prepare_rollback(source,change,target,directory):
    _validate_identity(source,change,target)
    path=directory/(change.request_id+'-native.json')
    if not path.exists():raise CategoryConflict('rollback',code='journal_missing')
    state=json.loads(path.read_text())
    if (state.get('version')!=JOURNAL_VERSION or state.get('request_id')!=change.request_id
            or state.get('spreadsheet_id')!=target.id or state.get('plan_signature')!=_signature(state)):
        raise CategoryConflict('rollback',code='legacy_pending_requires_operator')
    if state.get('source_digest')!=change.source_digest or state.get('target_digest')!=change.target_digest:
        raise CategoryConflict('rollback',code='plan_modified')
    if state.get('finished'):
        if state.get('phase')=='rolled_back':return path,state,None
        raise CategoryConflict('rollback',code='already_complete')
    current=read_google_spreadsheet(target)
    _require_owner(_meta_from(current),change,allow_ready=True,rollback_revision=state.get('rollback_revision'))
    projected=rollback_projection(source,change.output,current,state['steps'],target)
    for step in state['steps']:
        if step['kind']=='capacity':
            if not capacity_matches(read_capacity(target,step),step['before']):
                raise CategoryConflict('rollback_preflight',step['sheet'],code='grid_shrunk')
            continue
        if step['kind']!='cells' and view_state(target,step) not in (view_expected(step,'before'),view_expected(step,'after')):
            if (step['kind']=='validations' and step.get('phase')=='reverted' and state.get('rollback_dropdowns')
                    and view_state(target,step) in (view_expected(state['rollback_dropdowns'],'before'),view_expected(state['rollback_dropdowns'],'after'))):continue
            raise CategoryConflict('rollback_preflight',code='native_view_conflict')
    return path,state,projected


def rollback_change_to_google_copy(source,change,target,directory,*,confirmed):
    if confirmed is not True:raise MigrationError('Confirm rollback explicitly.')
    path,state,projected=prepare_rollback(source,change,target,directory)
    if state.get('phase')=='rolled_back':return
    try:
        if 'rollback_revision' not in state:
            state['rollback_revision']=change.expected_revision+2
            state['rollback_dropdowns']=validation_steps(target,projected)
        finalizing = state['phase']=='rollback_finalizing'
        meta_book=read_google_spreadsheet(target);meta=_meta_from(meta_book)
        if 'rollback_maintenance' not in state:
            state['rollback_maintenance']=_meta_step(meta_book,replace(meta,access_mode=AccessMode.MAINTENANCE,pending_request_id=change.request_id),'rollback_maintenance')
        state['phase']='rollback_finalizing' if finalizing else 'rolling_back'
        state['stage']='rollback_preflight';_private_json(path,state)
        if not finalizing:_run_step(target,state['rollback_maintenance'],state,path)
        for step in reversed(state['steps']):
            if step['kind']=='validations' and step.get('phase')=='reverted' and state.get('rollback_dropdowns',{}).get('phase')!='prepared':continue
            _run_step(target,step,state,path,inverse=True)
        # Coherent lists for retained manual input, rather than restoring stale
        # validation rules from an old row snapshot.
        state['rollback_dropdowns']=state.get('rollback_dropdowns') or validation_steps(target,projected)
        # Original validation may have been restored above; regenerate the plan
        # for this final step only before its first write, then persist it.
        if state['rollback_dropdowns']['phase']=='prepared':
            state['rollback_dropdowns']=validation_steps(target,projected);_private_json(path,state)
        _run_step(target,state['rollback_dropdowns'],state,path)
        current=read_google_spreadsheet(target);require_business(current,target.id)
        for step in state['steps']:
            if step['kind']=='cells':
                for edit in step['edits']:
                    if cell_state(current.sheets[step['sheet']],tuple(edit['key']),target)!=edit['before']:
                        raise CategoryConflict('rollback_verify',step['sheet'],tuple(edit['key']))
        if 'rollback_publish' not in state:
            meta=replace(_meta_from(source),taxonomy_revision=state['rollback_revision'])
            publish=_meta_step(source,meta,'rollback_publish')
            for edit,p in zip(publish['edits'],state['rollback_maintenance']['edits']):edit['before']=p['after']
            state['rollback_publish']=publish
        state['phase']='rollback_finalizing';_private_json(path,state)
        _run_step(target,state['rollback_publish'],state,path)
        _ready_metadata(target).require_ready(target.id,expected_revision=state['rollback_revision'])
        state.update(phase='rolled_back',finished=True,last_error=None);_private_json(path,state)
    except Exception as error:
        if not state.get('last_error') or isinstance(error,CategoryConflict):
            state['last_error']=safe_failure(error,state.get('stage','rollback'))
        _private_json(path,state);raise
