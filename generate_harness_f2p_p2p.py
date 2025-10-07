#!/usr/bin/env python3
import argparse, json, pathlib, shlex, subprocess, tempfile, sys, re
from typing import Dict, List

# Harness APIs
from swebench.harness.test_spec.python import get_test_directives
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

# ---------- helpers ----------
def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, text=True, check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout

def docker(image: str, inner: str, mounts: Dict[str,str], workdir="/work") -> str:
    m = " ".join(f"-v {shlex.quote(h)}:{shlex.quote(c)}" for h,c in mounts.items())
    return sh(f"docker run --rm -i {m} -w {shlex.quote(workdir)} {shlex.quote(image)} bash -lc {shlex.quote(inner)}")

def ensure_repo(image: str, mounts: Dict[str,str], repo_slug: str, base_commit: str, repo_dir="/work/repo"):
    print("== [1/6] Prepare repo at base_commit ==")
    script = f"""
set -e
if ! command -v git >/dev/null 2>&1; then
  (apt-get update && apt-get install -y git) >/dev/null 2>&1 || true
fi
if [ ! -d {repo_dir} ]; then
  mkdir -p {repo_dir}
  git clone --depth 1 https://github.com/{repo_slug}.git {repo_dir} >/dev/null 2>&1
fi
cd {repo_dir}
git fetch --all --tags >/dev/null 2>&1 || true
git checkout {base_commit} >/dev/null 2>&1 || git reset --hard {base_commit} >/dev/null 2>&1 || true
"""
    docker(image, script, mounts)

def write_text(host_root: pathlib.Path, rel: str, content: str):
    p = host_root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)

def apply_patch_in_container(image: str, mounts: Dict[str,str], repo_dir: str, base_commit: str, patch_text: str) -> bool:
    """
    Apply with the same fallback order SWE-bench uses:
      git apply  → git apply --reject → patch -p1 --fuzz=5
    """
    script = f"""
set -e
cd {repo_dir}
git reset --hard >/dev/null 2>&1 || true
(git checkout {base_commit} || git reset --hard {base_commit}) >/dev/null 2>&1 || true
git clean -fdx >/dev/null 2>&1 || true
cat >/work/patch.diff <<'EOF'
{patch_text}
EOF
if git apply --verbose /work/patch.diff >/dev/null 2>&1; then echo APPLY_OK; exit 0; fi
if git apply --verbose --reject /work/patch.diff >/dev/null 2>&1; then echo APPLY_OK; exit 0; fi
if patch --batch --fuzz=5 -p1 -i /work/patch.diff >/dev/null 2>&1; then echo APPLY_OK; exit 0; fi
echo APPLY_FAIL
"""
    out = docker(image, script, mounts, workdir="/")
    print(f'[out] = {out}')
    return "APPLY_OK" in out

def extract_tests_from_test_patch(test_patch: str, repo_slug: str) -> List[str]:
    # 1) ưu tiên API harness
    try:
        directives = get_test_directives({"repo": repo_slug.replace("/","__"), "test_patch": test_patch})
        if isinstance(directives, dict):
            tests = directives.get("test_directives") or directives.get("test_files") or []
        elif isinstance(directives, list):
            tests = directives
        else:
            tests = []
        if tests:
            return sorted(set(tests))
    except Exception:
        pass
    # 2) fallback grep từ diff header
    files = []
    for ln in test_patch.splitlines():
        m1 = re.search(r'^\+\+\+\s+b/(.+)$', ln)
        m2 = re.search(r'^---\s+a/(.+)$', ln)
        m3 = re.search(r'^diff --git a/(.+?) b/\1$', ln)
        cand = m1.group(1) if m1 else (m2.group(1) if m2 else (m3.group(1) if m3 else None))
        if cand and ("/tests/" in cand or cand.startswith(("tests/","test/"))) and cand.endswith(".py"):
            files.append(cand)
    return sorted(set(files))

def run_pytest_junit(image: str, mounts: Dict[str,str], repo_dir: str, tests: List[str], xml_rel: str):
    # pytest --junit-xml is the stable way to extract per-test status. :contentReference[oaicite:1]{index=1}
    files = " ".join(shlex.quote(f) for f in tests)
    cmd = f"cd {repo_dir} && PYTHONPATH={repo_dir} pytest -q -rA {files} --junit-xml={xml_rel} || true"
    docker(image, cmd, mounts)

def parse_junit(host_xml: pathlib.Path) -> Dict[str,str]:
    import xml.etree.ElementTree as ET
    res: Dict[str,str] = {}
    root = ET.parse(host_xml).getroot()
    for tc in root.iter('testcase'):
        file_ = tc.get('file') or tc.get('classname','').replace('.','/')
        name  = tc.get('name') or ''
        node  = f"{(file_ or '').lstrip('./')}::{name}"
        status = "PASSED"
        if tc.find('failure') is not None:
            status = "FAILED"
        elif tc.find('error') is not None:
            status = "ERROR"
        elif tc.find('skipped') is not None:
            status = "SKIPPED"
        res[node] = status
    return res

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_json", required=True)
    ap.add_argument("--container_repo_dir", default="/work/repo")
    args = ap.parse_args()

    data = json.loads(pathlib.Path(args.input_json).read_text())
    repo_slug = (data["repo"].replace("__","/") if "__" in data["repo"] else data["repo"]).strip()
    image      = data["image"]
    base       = data["base_commit"]
    test_patch = data["test_patch"]
    code_patch = data["patch"]
    version    = data.get("start_version") or data.get("version") or ""

    # lấy test_cmd cho repo/version từ harness (nếu có)
    specs = MAP_REPO_VERSION_TO_SPECS.get(repo_slug, {})
    spec  = {}
    if isinstance(specs, dict):
        # match theo tiền tố/chuỗi con cho các repo đặt version kiểu "2021.08.0"
        for k,v in specs.items():
            if k and k in version:
                spec = v; break
    test_cmd = spec.get("test_cmd", "pytest")  # script này hiện hỗ trợ pytest
    # (parser của harness dùng cho log text; ta dùng JUnit nên không cần ở đây)

    # Tạo mount bền /work để giữ repo & XML giữa các docker run
    host_work = pathlib.Path(tempfile.mkdtemp(prefix="swev_work_"))
    mounts = {str(host_work): "/work"}

    # [1] Chuẩn bị repo @ base
    ensure_repo(image, mounts, repo_slug, base, repo_dir=args.container_repo_dir)

    # [2] Ghi patch vào mount (để dễ debug nếu cần)
    print("== [2/6] Write patches into /work ==")
    write_text(host_work, "/test_patch.diff", test_patch)
    write_text(host_work, "/code_patch.diff", code_patch)

    # [3] Rút danh sách test từ test_patch
    print("== [3/6] Discover tests from test_patch ==")
    tests = extract_tests_from_test_patch(test_patch, repo_slug)
    if not tests:
        print("!! No tests found in test_patch; abort.")
        sys.exit(2)

    # [4] PRE: base + test_patch
    print("== [4/6] PRE: apply test_patch & run tests (JUnit) ==")
    if not apply_patch_in_container(image, mounts, args.container_repo_dir, base, test_patch):
        raise SystemExit("Apply test_patch failed (PRE).")
    run_pytest_junit(image, mounts, args.container_repo_dir, tests, "/work/pre.xml")

    # [5] POST: base + test_patch + gold patch
    print("== [5/6] POST: apply test_patch + gold patch & run tests (JUnit) ==")
    combo = test_patch + "\n\n" + code_patch
    if not apply_patch_in_container(image, mounts, args.container_repo_dir, base, combo):
        raise SystemExit("Apply (test_patch + gold patch) failed (POST).")
    run_pytest_junit(image, mounts, args.container_repo_dir, tests, "/work/post.xml")

    # [6] Parse & ghi kết quả
    print("== [6/6] Parse results & update JSON ==")
    pre  = parse_junit(host_work / "pre.xml")
    post = parse_junit(host_work / "post.xml")
    f2p = sorted([t for t,s in pre.items() if s in ("FAILED","ERROR") and post.get(t)=="PASSED"])
    p2p = sorted([t for t,s in pre.items() if s=="PASSED" and post.get(t)=="PASSED"])
    data["FAIL_TO_PASS"] = f2p
    data["PASS_TO_PASS"] = p2p
    pathlib.Path(args.input_json).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✅ Done. F2P={len(f2p)}, P2P={len(p2p)}")

if __name__ == "__main__":
    main()
