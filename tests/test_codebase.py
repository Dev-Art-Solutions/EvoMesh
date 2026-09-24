from evomesh.codebase import PACKAGE, package_root


def test_package_root_points_at_the_source_package(project_root):
    assert package_root(project_root) == project_root / "src" / PACKAGE
