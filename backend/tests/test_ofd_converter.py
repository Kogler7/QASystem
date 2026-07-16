from pathlib import Path

from algorithm.preproc.preproc_src.processors.ofd_converter import OFDConverter


def test_ofd_converter_invokes_configured_command(tmp_path, monkeypatch):
    source = tmp_path / "sample.ofd"
    source.write_bytes(b"ofd")
    converter = OFDConverter(command="ofd-to-pdf")
    output_dir = tmp_path / "conversion"
    output = output_dir / "sample.pdf"

    monkeypatch.setattr("shutil.which", lambda program: "/usr/bin/ofd-to-pdf" if program == "ofd-to-pdf" else None)

    def fake_run(command, **_kwargs):
        destination = Path(command[-1])
        destination.write_bytes(b"%PDF-test")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = converter.convert_to_pdf(str(source))

    assert result is not None
    assert Path(result).suffix == ".pdf"


def test_ofd_converter_reports_missing_runtime(tmp_path, monkeypatch):
    source = tmp_path / "sample.ofd"
    source.write_bytes(b"ofd")
    converter = OFDConverter(command="missing-ofd-to-pdf")
    monkeypatch.setattr("shutil.which", lambda _program: None)

    assert converter.convert_to_pdf(str(source)) is None
    assert "缺少 OFD 转换器" in (converter.last_conversion_error or "")
