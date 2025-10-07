#!/usr/bin/env python3
# generate_f2p_p2p.py
import argparse, json, os, subprocess, tempfile, shlex, pathlib, sys, re
from typing import List, Dict

# Optional: dùng get_test_directives nếu có (để tìm test từ test_patch)
from swebench.harness.test_spec.python import get_test_directives  # type: ignore

# ------------------------- utils -------------------------
def run(cmd: str, check=True, **popen_kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kwargs)

def docker_exec(image: str, inner_cmd: str, mounts: Dict[str,str], workdir="/workspace") -> str:
    # always mount a persistent host dir to /workspace so state survives across calls
    mflags = " ".join(f"-v {shlex.quote(h)}:{shlex.quote(c)}" for h, c in mounts.items())
    cmd = f"docker run --rm -i {mflags} -w {shlex.quote(workdir)} {shlex.quote(image)} bash -lc {shlex.quote(inner_cmd)}"
    res = run(cmd, check=False)
    return res.stdout

def ensure_repo(image: str, mounts: Dict[str,str], repo_slug: str, base_commit: str, repo_dir="/workspace/repo"):
    print("== [1/6] Prepare repo at base_commit ==")
    # nếu đã có code rồi thì bỏ qua clone
    out = docker_exec(image, f"test -d {repo_dir} && echo OK || true", mounts)
    if "OK" not in out:
        # cài git nếu thiếu; clone & checkout base
        setup = """
set -e
if ! command -v git >/dev/null 2>&1; then
  (apt-get update && apt-get install -y git) >/dev/null 2>&1 || true
fi
mkdir -p {repo_dir}
if [ ! -d {repo_dir}/.git ]; then
  git clone --depth 1 https://github.com/{repo_slug}.git {repo_dir}
fi
cd {repo_dir}
git fetch --depth 1 origin {base} || git fetch origin {base}
git checkout {base}
""".format(repo_dir=shlex.quote(repo_dir), repo_slug=repo_slug, base=base_commit)
        docker_exec(image, setup, mounts)
    # sanity: show HEAD (không in dài)
    docker_exec(image, f"cd {repo_dir} && git rev-parse --short=8 HEAD || true", mounts)

def write_text_to_container_mount(host_dir: str, relpath: str, text: str):
    path = pathlib.Path(host_dir) / relpath.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

def apply_patch_plain(image: str, mounts: Dict[str,str], target_dir: str, patch_rel: str) -> None:
    # thử nhiều biến thể patch(1)
    cmds = [
        f"cd {shlex.quote(target_dir)} && patch -p1 -t --fuzz=5 < {shlex.quote(patch_rel)} || true",
        f"cd {shlex.quote(target_dir)} && patch -p0 -t --fuzz=5 < {shlex.quote(patch_rel)} || true",
        f"cd {shlex.quote(target_dir)} && patch -l -p1 -t --fuzz=5 < {shlex.quote(patch_rel)} || true",  # ignore whitespace
    ]
    for c in cmds:
        out = docker_exec(image, c, mounts)
        if "Hunk #" in out or "patching file" in out:
            return
    raise RuntimeError("Apply failed: could not apply diff to " + target_dir)

def extract_test_files_from_diff(test_patch: str, repo_root_name_hint: str = "") -> List[str]:
    """
    Lấy danh sách file test từ test_patch.
    - Ưu tiên dùng get_test_directives (nếu có)
    - Fallback: grep các đường dẫn dưới tests/ hoặc */tests/*
    """
    if get_test_directives is not None:
        try:
            directives = get_test_directives({
                "repo": repo_root_name_hint,
                "test_patch": test_patch
            }) or {}
            files = directives
            if files:
                return sorted(set(files))

        except Exception:
            pass

    files = []
    for ln in test_patch.splitlines():
        # bắt cả '+++ b/...', '--- a/...', và dòng 'diff --git a/... b/...'
        m1 = re.search(r'^\+\+\+\s+b/(.+)$', ln)
        m2 = re.search(r'^---\s+a/(.+)$', ln)
        m3 = re.search(r'^diff --git a/(.+?) b/\1$', ln)
        cand = None
        if m1: cand = m1.group(1)
        elif m2: cand = m2.group(1)
        elif m3: cand = m3.group(1)
        if cand and ("/tests/" in cand or cand.startswith("tests/") or cand.startswith("test/")) and cand.endswith(".py"):
            files.append(cand)
    return sorted(set(files))

def run_pytest_to_junit(image: str, mounts: Dict[str,str], workdir: str, test_files: List[str], xml_out_rel: str):
    # dùng PYTHONPATH để ưu tiên working tree
    files = " ".join(shlex.quote(f) for f in test_files)
    cmd = f"cd {shlex.quote(workdir)} && PYTHONPATH={shlex.quote(workdir)} pytest -q -rA {files} --junit-xml={shlex.quote(xml_out_rel)} || true"
    docker_exec(image, cmd, mounts)

def parse_junit(xml_path: pathlib.Path) -> Dict[str, str]:
    import xml.etree.ElementTree as ET
    res: Dict[str, str] = {}
    tree = ET.parse(xml_path)
    root = tree.getroot()
    for tc in root.iter('testcase'):
        file_ = tc.get('file') or tc.get('classname', '').replace('.', '/')
        name  = tc.get('name') or ''
        node  = f"{file_}::{name}".lstrip("./")
        status = "PASSED"
        if tc.find('failure') is not None:
            status = "FAILED"
        elif tc.find('error') is not None:
            status = "ERROR"
        elif tc.find('skipped') is not None:
            status = "SKIPPED"
        res[node] = status
    return res

# ------------------------- main -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_json", required=True, help="File JSON một instance (repo/base_commit/patch/test_patch/image/...)")
    ap.add_argument("--overwrite", default=0, type=int, help="Overwrite or note")
    ap.add_argument("--container_repo_dir", default="/workspace/repo", help="Thư mục repo bên trong container (persist)")
    args = ap.parse_args()

    from pathlib import Path
    txt_file = Path("/mnt/data/swe_world_2/SWE-EVO/output_v4/not_done.txt")
    with open(txt_file, "r") as f:
        lines = [line.strip() for line in f if line.strip()]

    if args.overwrite == 0:
        if args.input_json not in lines:
            print(f'Overwrite mode is off and this sample has done => Skip this sample')
            return 0

    data = json.loads(pathlib.Path(args.input_json).read_text())
    # input fields
    image      = data["image"]
    base       = data["base_commit"]
    test_patch = data["test_patch"]
    code_patch = data["patch"]
    repo_slug  = (data["repo"].replace("__", "/") if "__" in data["repo"] else data["repo"]).strip()

    # persistent mount for the whole run
    persist = tempfile.mkdtemp(prefix="swev_persist_")
    mounts = {persist: "/workspace"}  # host_dir -> /workspace
    host_work = pathlib.Path(persist)

    # write patches into the mount once
    write_text_to_container_mount(persist, "/test_patch.diff", test_patch)
    write_text_to_container_mount(persist, "/code_patch.diff", code_patch)

    # 1) repo @ base_commit
    ensure_repo(image, mounts, repo_slug, base, repo_dir=args.container_repo_dir)

    # 2) make PRE/POST copies under /workspace
    print("== [2/6] Create PRE/POST workdirs ==")
    docker_exec(image, f"rm -rf /workspace/pre /workspace/post && mkdir -p /workspace/pre /workspace/post && cp -a {shlex.quote(args.container_repo_dir)}/. /workspace/pre/ && cp -a {shlex.quote(args.container_repo_dir)}/. /workspace/post/", mounts)

    # 3) apply test_patch to PRE & POST
    print("== [3/6] Apply test_patch (PRE & POST) ==")
    apply_patch_plain(image, mounts, "/workspace/pre",  "/workspace/test_patch.diff")
    apply_patch_plain(image, mounts, "/workspace/post", "/workspace/test_patch.diff")

    # 4) discover test files from test_patch
    test_files = extract_test_files_from_diff(test_patch, repo_slug) or []
    if not test_files:
        print("!! Không tìm thấy file test từ test_patch -> dừng")
        sys.exit(2)

    # 5) run PRE, then apply code patch and run POST
    print("== [4/6] Run PRE tests (JUnit) ==")
    run_pytest_to_junit(image, mounts, "/workspace/pre",  test_files, "/workspace/pre.xml")

    print("== [5/6] Apply code patch & run POST tests (JUnit) ==")
    apply_patch_plain(image, mounts, "/workspace/post", "/workspace/code_patch.diff")
    run_pytest_to_junit(image, mounts, "/workspace/post", test_files, "/workspace/post.xml")

    # 6) parse JUnit & compute F2P/P2P
    print("== [6/6] Parse results & update JSON ==")
    pre  = parse_junit(host_work / "pre.xml")
    post = parse_junit(host_work / "post.xml")

    f2p = sorted([t for t, s in pre.items()  if s in ("FAILED", "ERROR") and post.get(t) == "PASSED"])
    p2p = sorted([t for t, s in pre.items()  if s == "PASSED" and post.get(t) == "PASSED"])

    data["FAIL_TO_PASS"] = f2p
    data["PASS_TO_PASS"] = p2p

    pathlib.Path(args.input_json).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✅ Updated {args.input_json}: F2P={len(f2p)}, P2P={len(p2p)}")

if __name__ == "__main__":
    main()
