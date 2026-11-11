"""Identity and validation rules for a spreadsheet-local category registry."""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID, uuid4


class TaxonomyError(ValueError):
    """The category registry contains an invalid or ambiguous relationship."""


def normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def new_id() -> str:
    return str(uuid4())


def _validate_id(value: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaxonomyError("Registry IDs must be UUID strings.") from exc
    if str(parsed) != value:
        raise TaxonomyError("Registry IDs must use canonical UUID notation.")


@dataclass(frozen=True, order=True)
class Period:
    year: int
    month: int

    def __post_init__(self) -> None:
        if (
            type(self.year) is not int
            or type(self.month) is not int
            or not 1 <= self.year <= 9999
            or not 1 <= self.month <= 12
        ):
            raise TaxonomyError("Invalid reporting period.")

    @classmethod
    def parse(cls, value: str) -> Period:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
            raise TaxonomyError("A period must use YYYY-MM.")
        return cls(int(value[:4]), int(value[5:]))

    def __str__(self) -> str:
        return f"{self.year:04}-{self.month:02}"


@dataclass(frozen=True)
class Category:
    category_id: str
    transaction_type: str
    name: str
    active: bool = True
    deactivated_period: Period | None = None

    def __post_init__(self) -> None:
        _validate_id(self.category_id)
        if self.transaction_type not in {"Расход", "Доход"}:
            raise TaxonomyError("Unknown category transaction type.")
        if not isinstance(self.name, str) or not normalized_name(self.name):
            raise TaxonomyError("Category names must not be empty.")
        if type(self.active) is not bool:
            raise TaxonomyError("Category activity must be boolean.")
        if self.deactivated_period is not None and not isinstance(
            self.deactivated_period, Period
        ):
            raise TaxonomyError("Invalid category deactivation period.")
        if self.active == (self.deactivated_period is not None):
            raise TaxonomyError("Only inactive categories require a deactivation period.")


@dataclass(frozen=True)
class Subcategory:
    subcategory_id: str
    current_category_id: str
    name: str
    active: bool = True

    def __post_init__(self) -> None:
        _validate_id(self.subcategory_id)
        _validate_id(self.current_category_id)
        if not isinstance(self.name, str) or not normalized_name(self.name):
            raise TaxonomyError("Subcategory names must not be empty.")
        if type(self.active) is not bool:
            raise TaxonomyError("Subcategory activity must be boolean.")


@dataclass(frozen=True)
class HistoricalPair:
    """An operation's category remains valid after its subcategory moves."""

    category_id: str
    subcategory_id: str


@dataclass(frozen=True)
class Taxonomy:
    spreadsheet_id: str
    revision: int
    categories: tuple[Category, ...]
    subcategories: tuple[Subcategory, ...]
    historical_pairs: tuple[HistoricalPair, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.spreadsheet_id, str) or not self.spreadsheet_id.strip():
            raise TaxonomyError("A registry must belong to a spreadsheet.")
        if type(self.revision) is not int or self.revision < 0:
            raise TaxonomyError("A taxonomy revision must be a nonnegative integer.")
        # Freeze iterable inputs too: a frozen dataclass must not hide mutable lists.
        for name in ("categories", "subcategories", "historical_pairs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        categories = {item.category_id: item for item in self.categories}
        subcategories = {item.subcategory_id: item for item in self.subcategories}
        if len(categories) != len(self.categories) or len(subcategories) != len(
            self.subcategories
        ) or categories.keys() & subcategories.keys():
            raise TaxonomyError("Registry IDs must be unique across entities.")
        category_names: set[tuple[str, str]] = set()
        for item in self.categories:
            key = (item.transaction_type, normalized_name(item.name))
            if key in category_names:
                raise TaxonomyError("Ambiguous category name within a transaction type.")
            category_names.add(key)
        subcategory_names: set[tuple[str, str]] = set()
        for item in self.subcategories:
            if item.current_category_id not in categories:
                raise TaxonomyError("A subcategory refers to an unknown category.")
            key = (item.current_category_id, normalized_name(item.name))
            if key in subcategory_names:
                raise TaxonomyError("Ambiguous subcategory name within a category.")
            subcategory_names.add(key)
        pairs: set[HistoricalPair] = set()
        for pair in self.historical_pairs:
            if pair in pairs:
                raise TaxonomyError("Duplicate historical pair.")
            pairs.add(pair)
            if pair.category_id not in categories or pair.subcategory_id not in subcategories:
                raise TaxonomyError("Historical pair refers to an unknown entity.")
            child = subcategories[pair.subcategory_id]
            if categories[pair.category_id].transaction_type != categories[
                child.current_category_id
            ].transaction_type:
                raise TaxonomyError("A historical pair cannot cross transaction types.")

    def assert_scope(self, spreadsheet_id: str, expected_revision: int) -> None:
        if spreadsheet_id != self.spreadsheet_id or expected_revision != self.revision:
            raise TaxonomyError("The registry scope or revision has changed.")

    def category(self, category_id: str) -> Category:
        for item in self.categories:
            if item.category_id == category_id:
                return item
        raise TaxonomyError("Unknown category ID.")

    def subcategory(self, subcategory_id: str) -> Subcategory:
        for item in self.subcategories:
            if item.subcategory_id == subcategory_id:
                return item
        raise TaxonomyError("Unknown subcategory ID.")

    def validate_pair(
        self, category_id: str, subcategory_id: str, *, new_operation: bool
    ) -> HistoricalPair:
        category = self.category(category_id)
        subcategory = self.subcategory(subcategory_id)
        pair = HistoricalPair(category_id, subcategory_id)
        current = subcategory.current_category_id == category_id
        if new_operation:
            if not current or not category.active or not subcategory.active:
                raise TaxonomyError("This pair is not available for a new operation.")
        elif not current and pair not in self.historical_pairs:
            raise TaxonomyError("This historical pair is not registered.")
        return pair

    def active_pairs(self) -> tuple[HistoricalPair, ...]:
        return tuple(
            HistoricalPair(item.current_category_id, item.subcategory_id)
            for item in self.subcategories
            if item.active and self.category(item.current_category_id).active
        )
