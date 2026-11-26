from zipfile import ZipFile

from scripts.build_lab import wheel_modules, wheel_names


def test_wheel_import_names_require_identifier_components(tmp_path):
    wheel = tmp_path / "demo.whl"
    with ZipFile(wheel, "w") as archive:
        for member in (
            "demo/__init__.py",
            "demo/x.data/__init__.py",
            "demo/x.data/y.py",
            "demo/class/x.py",
            "demo/for.py",
            "demo/dotted.stem.py",
            "demo/x.y/_ext.cpython-314-x86_64-linux-gnu.so",
            "demo/_invalid.stem.abi3.so",
            "demo/_ok.abi3.so",
        ):
            archive.writestr(member, "")

    assert wheel_modules(wheel) == (["demo", "demo._ok"], ["demo._ok"], 1)
    assert wheel_names(wheel) == {"modules": ["demo._ok"], "packages": ["demo"]}
