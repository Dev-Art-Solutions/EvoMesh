from evomesh.codebase import (
    IMPROVEMENTS_FILE,
    PACKAGE,
    Improvement,
    Step,
    fabricated_references,
    known_dead,
    package_root,
    plan_needle,
    plan_objective,
    step_needle,
    stray_root_files,
    survey,
)


def test_package_root_points_at_the_source_package(project_root):
    assert package_root(project_root) == project_root / "src" / PACKAGE


def test_fabricated_references_leaves_a_real_reference_alone(project_root):
    assert fabricated_references(
        "use `evomesh.codebase.package_root` from the package", project_root
    ) == []


def test_survey_reports_the_load_bearing_modules(project_root):
    modules = {module.name for module in survey(project_root)}
    assert "codebase" in modules

    base = next(module for module in survey(project_root) if module.name == "codebase")
    assert base.path == package_root(project_root) / "codebase.py"


def test_known_dead_returns_empty_when_no_baseline_file(tmp_path):
    assert known_dead(tmp_path) == frozenset()


def test_known_dead_read_and_skips_comments(tmp_path):
    baseline = tmp_path / "docs" / "evolution" / "known-dead-modules.txt"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("# stale orphans\nreachability\n\n\norchestration.py\n")
    assert known_dead(tmp_path) == frozenset({"reachability", "orchestration.py"})


def test_stray_root_files_lists_a_file_left_at_the_project_root(tmp_path):
    (tmp_path / "orphan.py").write_text("# left behind by accident\n")
    assert stray_root_files(tmp_path) == ["orphan.py"]


def test_plan_needle_prefixed_with_the_objective_prefix():
    assert plan_needle(Improvement("Wire up `known_dead", "In codebase.")) == (
        "Plan this improvement to EvoMesh: Wire up `known_dead"
    )


def test_plan_objective_uses_the_item_title_on_its_own_line():
    assert plan_objective(Improvement("Split the DB module")) == "\n".join(
        [
            "Plan this improvement to EvoMesh: Split the DB module",
            f"Split this backlog item into 1 to 3 small steps and write them under it "
            f"in {IMPROVEMENTS_FILE.as_posix()}. Do not change any code: each step "
            "becomes one later generation's whole objective.",
            "THE ITEM: Split the DB module",
        ]
    )


def test_step_needle_prefixed_with_the_improvement_needle_and_step_number():
    assert step_needle(
        Improvement("Split the DB module"), Step(number=3, path="", symbol="", change="")
    ) == (
        "Implement this improvement to EvoMesh: Split the DB module [step 3]"
    )
