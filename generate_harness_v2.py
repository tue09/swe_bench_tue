from pathlib import Path
import json
import docker, json, pathlib
import argparse

from docker.errors import ImageNotFound, APIError
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
from swebench.harness.constants import KEY_INSTANCE_ID, KEY_MODEL, KEY_PREDICTION
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
from swebench.harness.grading import get_logs_eval, TestStatus
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.run_evaluation import build_env_images, run_instances
from swebench.harness.constants import RUN_EVALUATION_LOG_DIR

parser = argparse.ArgumentParser(description="Read CLI input")
parser.add_argument("--instance", type=str, default='...', help="xxx")
parser.add_argument("--max_workers", type=int, default='4', help="xxx")

args = parser.parse_args()

def merge_patches(code_patch: str, test_patch: str, order: str = "test_then_code") -> str:
    """
    return an only unified-diff which are merged from test_patch and code_patch.
    order: "test_then_code" (mặc định) hoặc "code_then_test".
    """
    def clean(x: str) -> str:
        if not x:
            return ""
        x = x.replace("\r\n", "\n").replace("\r", "\n")
        if not x.endswith("\n"):
            x += "\n"
        return x

    cp = clean(code_patch)
    tp = clean(test_patch)

    parts = []
    if order == "test_then_code":
        if tp: parts.append(tp)
        if cp: parts.append(cp)
    else:
        if cp: parts.append(cp)
        if tp: parts.append(tp)

    return ("\n".join(p.rstrip("\n") for p in parts if p) + "\n") if parts else ""


def pull_and_retag_instance_images(client, instances, target_tag="latest"):
    for ins in instances:
        src_ref = ins["image"]               
        iid     = ins["instance_id"]         
        dst_repo = f"sweb.eval.x86_64.{iid}" 
        dst_ref  = f"{dst_repo}:{target_tag}"

        try:
            client.images.get(dst_ref)
            print(f"[skip] already present: {dst_ref}")
            continue
        except ImageNotFound:
            pass
        print(f"[pull] {src_ref}")
        img = client.images.pull(src_ref)    
        print(f"[tag ] {src_ref}  ->  {dst_ref}")
        client.api.tag(image=img.id, repository=dst_repo, tag=target_tag)
    print("[ok] retag all instance images done.")

root_path = "/mnt/data/swe_world_2/SWE-EVO/output_v7"
root = Path(root_path)
instances = []
count = 0
for p in root.glob("*.json"):
    if args.instance != '...':
        if p != Path(f"/mnt/data/swe_world_2/SWE-EVO/output_v7/{args.instance}.json"): 
            continue
    d = json.loads(p.read_text())
    current_version = d.get("end_version") or d.get("version")
    true_version = current_version
    specs_by_ver = MAP_REPO_VERSION_TO_SPECS.get(d["repo"], {}) 
    found = False
    for ver_harness in specs_by_ver.keys(): 
        if ver_harness in current_version: 
            true_version = ver_harness
            found = True
    if found == False:
        print(f'Cannot find true version in current rule based !!! Exit with current_version = {current_version} and total_keys = {specs_by_ver.keys()}')
        exit()

    test_cmd = MAP_REPO_VERSION_TO_SPECS[d["repo"]][true_version]["test_cmd"]
    log_parser = MAP_REPO_TO_PARSER[d["repo"]]
    print(f'[log_parser.__name__] = {log_parser.__name__} with repo = {d["repo"]} and [test_cmd] = {test_cmd} and [version] = {true_version}')
    instances.append(d)    
    instances[count]["version"] = true_version
    instances[count]["test_cmds"] = test_cmd
    print(f'[test_cmd] = {instances[count]["test_cmds"]}')
    instances[count]["log_parser"] = log_parser.__name__

    instances[count]["all_patch"] = merge_patches(
        instances[count]["patch"],
        instances[count]["test_patch"],
        order="test_then_code",  # or "code_then_test"
    )
    instances[count]["code_patch"] = instances[count]["patch"]

    count += 1


#########################################################################
# Run instance for gold_patch
predictions = {}
for inst in instances:
    print(f'inst["instance_id"] = {inst["instance_id"]}')
    predictions[inst["instance_id"]] = {
        KEY_MODEL: "gold",        
        KEY_PREDICTION: inst["all_patch"],   # before: inst["patch"]
        KEY_INSTANCE_ID: inst["instance_id"]
    }

run_id = "sweworld-gold-api"
cache_level = "env"      # none|base|env|instance
clean = True
force_rebuild = False
max_workers = args.max_workers
timeout = 1800
namespace = None
instance_image_tag = "latest"
rewrite_reports = False
client = docker.from_env()

pull_and_retag_instance_images(client, instances, target_tag=instance_image_tag)

if namespace is None and not rewrite_reports:
    build_env_images(client, instances, force_rebuild, max_workers)

run_instances(
    predictions,
    instances,
    cache_level,
    clean,
    force_rebuild,
    max_workers,
    run_id,
    timeout,
    namespace=namespace,
    instance_image_tag=instance_image_tag,
    rewrite_reports=rewrite_reports,
)

print(f"Logs & reports in: {RUN_EVALUATION_LOG_DIR}")

#########################################################################
# Run instance for pre gold_patch

instances = []
count = 0
for p in root.glob("*.json"):
    if args.instance != '...':
        if p != Path(f"/mnt/data/swe_world_2/SWE-EVO/output_v7/{args.instance}.json"): 
            continue
    d = json.loads(p.read_text())
    current_version = d.get("start_version") or d.get("version")
    true_version = current_version
    specs_by_ver = MAP_REPO_VERSION_TO_SPECS.get(d["repo"], {}) 
    found = False
    for ver_harness in specs_by_ver.keys(): 
        if ver_harness in current_version: 
            true_version = ver_harness
            found = True
    if found == False:
        print(f'Cannot find true version in current rule based !!! Exit with current_version = {current_version} and total_keys = {specs_by_ver.keys()}')
        exit()

    test_cmd = MAP_REPO_VERSION_TO_SPECS[d["repo"]][true_version]["test_cmd"]
    log_parser = MAP_REPO_TO_PARSER[d["repo"]]
    print(f'[log_parser.__name__] = {log_parser.__name__} with repo = {d["repo"]} and [test_cmd] = {test_cmd} and [version] = {true_version}')
    instances.append(d)    
    instances[count]["version"] = true_version
    test_cmd = test_cmd.replace("pytest", "pytest --continue-on-collection-errors", 1)
    print(f'[test_cmd] = {test_cmd}')
    instances[count]["test_cmds"] = test_cmd
    instances[count]["log_parser"] = log_parser.__name__
    instances[count]["all_patch"] = merge_patches(
        instances[count]["patch"],
        instances[count]["test_patch"],
        order="test_then_code",  # or "code_then_test"
    )
    count += 1

print(f'[len] = {len(instances)}')
predictions_pre = {}
for inst in instances:
    predictions_pre[inst["instance_id"]] = {
        KEY_MODEL: "pre-empty-patch",        
        KEY_PREDICTION: inst["test_patch"] , # before: ""
        KEY_INSTANCE_ID: inst["instance_id"]
    }

run_id = "sweworld-pre-empty"
cache_level = "env"    
clean = True
force_rebuild = False
max_workers = args.max_workers
timeout = 1800
namespace = None
instance_image_tag = "latest"
rewrite_reports = False
client = docker.from_env()

pull_and_retag_instance_images(client, instances, target_tag=instance_image_tag)

if namespace is None and not rewrite_reports:
    build_env_images(client, instances, force_rebuild, max_workers)

run_instances(
    predictions=predictions_pre,
    instances=instances,
    cache_level=cache_level,
    clean=clean,
    force_rebuild=force_rebuild,
    max_workers=max_workers,
    run_id=run_id,   
    timeout=timeout,
    namespace=namespace,
    instance_image_tag=instance_image_tag,
    rewrite_reports=rewrite_reports,
)

print(f"Logs & reports in: {RUN_EVALUATION_LOG_DIR}")

#########################################################################
# Parse for F2P and P2P

for inst in instances:
    instance_json = Path(root_path + "/" + inst["instance_id"] + '.json')
    spec = make_test_spec(inst, namespace=None)

    post_log = Path(f"/mnt/data/swe_world_2/SWE-bench/logs/run_evaluation/sweworld-gold-api/gold/{inst["instance_id"]}/test_output.txt")
    pre_log  = Path(f"/mnt/data/swe_world_2/SWE-bench/logs/run_evaluation/sweworld-pre-empty/pre-empty-patch/{inst["instance_id"]}/test_output.txt")

    post_map, found_post = get_logs_eval(spec, str(post_log))
    pre_map,  found_pre  = get_logs_eval(spec, str(pre_log))

    assert found_post and found_pre, "Cannot read test_output.txt"

    # Compute F2P, P2P
    F = TestStatus.FAILED.value
    E = TestStatus.ERROR.value
    P = TestStatus.PASSED.value

    f2p = sorted(
        t for t, s_post in post_map.items()
        if s_post == P and (pre_map.get(t) in (F, E, None))
    )

    p2p = sorted(
        t for t, s_pre in pre_map.items()
        if s_pre == P and post_map.get(t) == P
    )

    inst["FAIL_TO_PASS"] = f2p
    inst["PASS_TO_PASS"] = p2p
    instance_json.write_text(json.dumps(inst, ensure_ascii=False, indent=2))
    print(f"Done: F2P={len(f2p)}, P2P={len(p2p)}")


