from pathlib import Path

from scripts import gap_wheels


OPTOUT = Path(__file__).resolve().parents[1] / "data" / "gap-wheels-optout.txt"


def test_opted_out_missing_file(tmp_path):
    assert gap_wheels.opted_out(tmp_path / OPTOUT.name) == set()


def test_opted_out_reads_project_names(tmp_path):
    path = tmp_path / OPTOUT.name
    path.write_text(
        "  # Opted-out projects\n\n \t\n PyYAML  \npyyaml\nFoo_Bar.baz\nnumpy  # asked in #12\n",
        encoding="utf-8",
    )
    assert gap_wheels.opted_out(path) == {"pyyaml", "foo-bar-baz", "numpy"}


def test_committed_optout_file_has_valid_project_names():
    assert OPTOUT.is_file()
    gap_wheels.opted_out(OPTOUT)


def test_opted_out_rejects_a_version_in_a_copy_of_the_committed_file(tmp_path):
    lines = OPTOUT.read_text(encoding="utf-8").splitlines()
    path = tmp_path / OPTOUT.name
    path.write_text("\n".join([*lines, "PyYAML 6.0", ""]), encoding="utf-8")

    try:
        gap_wheels.opted_out(path)
    except ValueError as error:
        assert str(error).startswith(f"{path}:{len(lines) + 1}:")
        assert "PyYAML 6.0" in str(error)
    else:
        raise AssertionError("opted_out accepted 'PyYAML 6.0' as a project name")
