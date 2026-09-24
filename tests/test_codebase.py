from evomesh.codebase import PACKAGE, package_root, survey


def test_package_root_points_at_the_source_package(project_root):
    assert package_root(project_root) == project_root / "src" / PACKAGE


def test_survey_reports_the_load_bearing_modules(project_root):
    modules = {module.name for module in survey(project_root)}
    assert "codebase" in modules

    base = next(module for module in survey(project_root) if module.name == "codebase")
    assert base.path == package_root(project_root) / "codebase.py"
