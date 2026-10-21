"""Кэш рабочей книги Excel: и ускорение, и сохранение корректности."""
import parser.export.excel as excel_module
from models import Item
from parser.export.excel import ExcelStorage


def test_repeated_saves_accumulate_rows(tmp_path):
    path = tmp_path / "out.xlsx"
    storage = ExcelStorage(path)

    storage.save([Item(id=1, title="a"), Item(id=2, title="b"), Item(id=3, title="c")])
    storage.save([Item(id=4, title="d")])

    sheet = ExcelStorage(path)._get_workbook().active
    assert sheet.max_row == 1 + 4  # заголовок + 4 строки


def test_load_workbook_called_once_across_saves(tmp_path, monkeypatch):
    """Ключевая оптимизация: файл перечитывается с диска только один раз."""
    path = tmp_path / "out.xlsx"
    storage = ExcelStorage(path)

    calls = {"n": 0}
    real_load = excel_module.load_workbook

    def counting_load(*args, **kwargs):
        calls["n"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(excel_module, "load_workbook", counting_load)

    storage.save([Item(id=1, title="a")])
    storage.save([Item(id=2, title="b")])
    storage.save([Item(id=3, title="c")])

    assert calls["n"] == 1


def test_appends_to_existing_external_file(tmp_path):
    path = tmp_path / "existing.xlsx"
    # файл, созданный отдельным прогоном (заголовок + строка)
    storage_first = ExcelStorage(path)
    storage_first.save([Item(id=1, title="a")])
    # сбросить кэш — имитация нового процесса, работающего с тем же файлом
    stub = ExcelStorage(path)
    stub._workbook = None

    stub.save([Item(id=2, title="b")])
    sheet = stub._get_workbook().active
    assert sheet.max_row == 1 + 2


def test_empty_save_is_noop(tmp_path):
    path = tmp_path / "out.xlsx"
    storage = ExcelStorage(path)
    storage.save([])
    storage.save([Item(id=1, title="a")])
    sheet = storage._get_workbook().active
    assert sheet.max_row == 2


def test_save_persists_to_disk_each_call(tmp_path):
    """Устойчивость к сбоям сохраняется: файл на диске обновляется после
    каждого вызова save() (страницы не теряются при падении)."""
    path = tmp_path / "out.xlsx"
    storage = ExcelStorage(path)
    storage.save([Item(id=1, title="a")])

    independent = excel_module.load_workbook(path).active
    assert independent.max_row == 2
