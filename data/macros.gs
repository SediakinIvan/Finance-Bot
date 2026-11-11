function saveMonthlyReport() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var traity = ss.getSheetByName("Траты");
  var analitika = ss.getSheetByName("Аналитика");
  var svod = ss.getSheetByName("Архив");
  
  // Берём месяц и год из ячеек D1 и D2 на листе Аналитика
  var monthNum = analitika.getRange("D1").getValue();
  var yearNum = analitika.getRange("D2").getValue();
  
  // Формируем название месяца (текстом)
  var monthNames = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", 
                    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  var monthName = monthNames[monthNum - 1] + " " + yearNum;
  
  // ========== 1. СВОДНАЯ ТАБЛИЦА ==========
  if (!svod) {
    svod = ss.insertSheet("Архив");
    var headers = ["Месяц", "Доход", "Расход", "Остаток", 
                   "Еда офис", "Продукты", "Транспорт", "Вб+Озон", 
                   "Спорт", "Подписки", "Кафе/Рестораны", "Отдых/Развлечения",
                   "Вадим", "REST", "Здоровье", "Квартира", "Развитие", "Путешествия"];
    svod.getRange(1, 1, 1, headers.length).setValues([headers]);
    svod.getRange(1, 1, 1, headers.length).setFontWeight("bold");
  }
  
  // Берём данные с листа Аналитика (B3, B4, B5, и B9:B22)
  var расход = analitika.getRange("B3").getValue();
  var доход = analitika.getRange("B4").getValue();
  var остаток = analitika.getRange("B5").getValue();
  
  // Категории (строки 9–22)
  var edaOffice = analitika.getRange("B9").getValue();    // Еда офис
  var products = analitika.getRange("B10").getValue();    // Продукты
  var transport = analitika.getRange("B11").getValue();   // Транспорт
  var wbOzon = analitika.getRange("B12").getValue();      // Вб+Озон
  var sport = analitika.getRange("B13").getValue();       // Спорт
  var subscriptions = analitika.getRange("B14").getValue(); // Подписки
  var cafe = analitika.getRange("B15").getValue();        // Кафе/Рестораны
  var rest = analitika.getRange("B16").getValue();        // Отдых/Развлечения
  var vadim = analitika.getRange("B17").getValue();       // Вадим
  var restCategory = analitika.getRange("B18").getValue(); // REST
  var health = analitika.getRange("B19").getValue();      // Здоровье
  var apartment = analitika.getRange("B20").getValue();   // Квартира
  var development = analitika.getRange("B21").getValue(); // Развитие
  var travel = analitika.getRange("B22").getValue();      // Путешествия
  
  // Находим последнюю строку в сводке
  var lastRow = svod.getLastRow();
  var newRow = lastRow + 1;
  
  // Проверяем, не было ли уже записи за этот месяц (чтобы не дублировать)
  if (lastRow >= 1) {
    var existingData = svod.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = 0; i < existingData.length; i++) {
      if (existingData[i][0] == monthName) {
        var response = Browser.msgBox("Внимание", 
          "Запись за " + monthName + " уже существует. Перезаписать?", 
          Browser.Buttons.YES_NO);
        if (response == "no") {
          return;
        } else {
          svod.deleteRow(i + 2);
          newRow = i + 2;
          break;
        }
      }
    }
  }
  
  // Записываем строку в сводку
  svod.getRange(newRow, 1).setValue(monthName);
  svod.getRange(newRow, 2).setValue(доход);
  svod.getRange(newRow, 3).setValue(расход);
  svod.getRange(newRow, 4).setValue(остаток);
  svod.getRange(newRow, 5).setValue(edaOffice);
  svod.getRange(newRow, 6).setValue(products);
  svod.getRange(newRow, 7).setValue(transport);
  svod.getRange(newRow, 8).setValue(wbOzon);
  svod.getRange(newRow, 9).setValue(sport);
  svod.getRange(newRow, 10).setValue(subscriptions);
  svod.getRange(newRow, 11).setValue(cafe);
  svod.getRange(newRow, 12).setValue(rest);
  svod.getRange(newRow, 13).setValue(vadim);
  svod.getRange(newRow, 14).setValue(restCategory);
  svod.getRange(newRow, 15).setValue(health);
  svod.getRange(newRow, 16).setValue(apartment);
  svod.getRange(newRow, 17).setValue(development);
  svod.getRange(newRow, 18).setValue(travel);
  
  // ========== 2. ПОЛНАЯ КОПИЯ ЛИСТА (ЗНАЧЕНИЯ, НЕ ФОРМУЛЫ!) ==========
  var fullArchiveName = "Архив (" + monthName + ")";
  var existingSheet = ss.getSheetByName(fullArchiveName);
  
  if (existingSheet) {
    var response = Browser.msgBox("Внимание", 
      "Лист " + fullArchiveName + " уже существует. Перезаписать?", 
      Browser.Buttons.YES_NO);
    if (response == "yes") {
      ss.deleteSheet(existingSheet);
    } else {
      // Если не перезаписываем, всё равно продолжаем с очисткой Траты
      // Но пропускаем создание копии
      var skipCopy = true;
    }
  }
  
  if (!skipCopy) {
    // Создаём новый лист
    var archiveSheet = ss.insertSheet(fullArchiveName);
    
    // Получаем ВСЕ данные с листа Аналитика (включая числа, текст, но без формул)
    var analitikaRange = analitika.getDataRange();
    var analitikaValues = analitikaRange.getValues();
    
    // Вставляем ТОЛЬКО ЗНАЧЕНИЯ (без формул)
    archiveSheet.getRange(1, 1, analitikaValues.length, analitikaValues[0].length).setValues(analitikaValues);
    
    // Копируем ширину столбцов
    for (var col = 1; col <= analitikaValues[0].length; col++) {
      var width = analitika.getColumnWidth(col);
      archiveSheet.setColumnWidth(col, width);
    }
    
    // Копируем формат чисел (для процентов и т.д.)
    var sourceRange = analitika.getRange(1, 1, analitikaValues.length, analitikaValues[0].length);
    sourceRange.copyTo(archiveSheet.getRange(1, 1, analitikaValues.length, analitikaValues[0].length), 
                       SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    
    // Перемещаем лист в конец
    ss.setActiveSheet(archiveSheet);
    ss.moveActiveSheet(ss.getNumSheets());
  }
  
  // ========== 3. ОЧИСТКА ТРАТ ==========
  var lastRowTraity = traity.getLastRow();
  if (lastRowTraity > 1) {
    traity.getRange(2, 1, lastRowTraity - 1, traity.getLastColumn()).clearContent();
  }
  
  SpreadsheetApp.getUi().alert("✅ Архивация завершена!\n\n" +
    "Месяц: " + monthName + "\n" +
    "1. Строка добавлена/обновлена в листе 'Архив'\n" +
    "2. Создан лист с цифрами (без формул): " + fullArchiveName + "\n" +
    "3. Лист 'Траты' очищен для новых записей");
}