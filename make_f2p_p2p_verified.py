# save as make_f2p_p2p_verified.py
import os, subprocess, shlex, json, tempfile, pathlib, textwrap

from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
from swebench.harness.constants.python import MAP_REPO_VERSION_TO_SPECS_PY
from swebench.harness.test_spec.python import get_test_directives
from swebench.harness.constants import (
    TestStatus, FAIL_TO_PASS, PASS_TO_PASS,
    RUN_EVALUATION_LOG_DIR, LOG_TEST_OUTPUT
)

from swebench.harness.log_parsers import MAP_REPO_TO_PARSER

REPO_SLUG   = os.environ["REPO_SLUG"]
BASE_COMMIT = os.environ["BASE_COMMIT"]
VERSION     = os.environ.get("VERSION", "latest")

TEST_PATCH = pathlib.Path("tmp/test_patch.diff").read_text()
PATCH      = pathlib.Path("tmp/patch.diff").read_text()

# 1) Lấy lệnh test đúng repo/version (ví dụ pytest, hoặc pytest với flags đặc thù)
print(f'type = {type(MAP_REPO_VERSION_TO_SPECS[REPO_SLUG])}')
print(f'key = {MAP_REPO_VERSION_TO_SPECS[REPO_SLUG].keys()}')
print(f'value = {MAP_REPO_VERSION_TO_SPECS[REPO_SLUG]["2.3"]["test_cmd"]}')

# test_cmd = MAP_REPO_VERSION_TO_SPECS[REPO_SLUG][VERSION]["test_cmd"]  # e.g. "pytest -q"
test_cmd = "pytest -rA"
# 2) Suy ra danh sách test/file cần chạy từ nội dung test_patch
test_targets = get_test_directives({"repo": REPO_SLUG, "test_patch": TEST_PATCH})  # list[str]

def run(cmd, cwd="testbed"):
    res = subprocess.run(cmd, cwd=cwd, shell=True, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return res.returncode, res.stdout

def reset_to_base():
    run(f"git reset --hard {shlex.quote(BASE_COMMIT)}")
    run("git clean -ffd")

def apply_diff(diff_text, path="tmp/tmp.diff"):
    pathlib.Path(path).write_text(diff_text)
    # kiểm tra trước khi apply để log rõ lỗi
    rc, out = run(f"git apply --check {shlex.quote(path)}")
    if rc != 0:
        raise RuntimeError("git apply --check failed:\n" + out)
    rc, out = run(f"git apply {shlex.quote(path)}")
    if rc != 0:
        raise RuntimeError("git apply failed:\n" + out)

def run_tests():
    # Chạy đúng subset test mà harness chỉ ra
    cmd = " ".join([test_cmd] + test_targets) + " || true"
    return run(cmd)

def parse_status_map(test_output: str):
    parser = MAP_REPO_TO_PARSER[REPO_SLUG]
    return parser(test_output)   # dict[test_name] -> "PASSED"/"FAILED"/"ERROR"/"XFAIL"/...

def compute_sets(pre_map, post_map):
    def passed(sm, t): return sm.get(t) in [TestStatus.PASSED.value, TestStatus.XFAIL.value]
    def failed(sm, t): return (t not in sm) or (sm.get(t) in [TestStatus.FAILED.value, TestStatus.ERROR.value])

    tests = set(pre_map.keys()) | set(post_map.keys())
    f2p = sorted([t for t in tests if failed(pre_map, t) and passed(post_map, t)])
    p2p = sorted([t for t in tests if passed(pre_map, t) and passed(post_map, t)])
    return f2p, p2p

def main():
    # --- Run 1: BEFORE FIX (base + test_patch) ---
    reset_to_base()
    apply_diff(TEST_PATCH, "tmp/test_patch.diff")
    _, pre_out = run_tests()
    pre_map = parse_status_map(pre_out)

    # --- Run 2: AFTER FIX (base + test_patch + patch) ---
    reset_to_base()
    apply_diff(TEST_PATCH, "tmp/test_patch.diff")
    apply_diff(PATCH, "tmp/patch.diff")
    _, post_out = run_tests()
    post_map = parse_status_map(post_out)

    f2p, p2p = compute_sets(pre_map, post_map)

    out = {
        "repo": REPO_SLUG,
        "base_commit": BASE_COMMIT,
        "VERSION": VERSION,
        "test_cmd": test_cmd,
        "targets": test_targets,
        "FAIL_TO_PASS": f2p,
        "PASS_TO_PASS": p2p,
        "debug": {
            "pre_counts": {k: list(pre_map.values()).count(k) for k in set(pre_map.values())},
            "post_counts": {k: list(post_map.values()).count(k) for k in set(post_map.values())},
        }
    }
    pathlib.Path("testbed/f2p_p2p_verified.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
