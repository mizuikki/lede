#!/usr/bin/env python3
"""Local regression tests for upstream candidate preparation and Actions control."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs
from unittest import mock
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SYNC = ROOT / "scripts/upstream-sync.py"
FEED_LOCK = ROOT / "scripts/feed-lock.py"
DISPATCH = ROOT / "scripts/sync-dispatch.py"
RESULT = ROOT / "scripts/sync-result.py"
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "verify_fork_contract", ROOT / "scripts/verify-fork-contract.py",
)
CONTRACT_MODULE = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(CONTRACT_MODULE)
RESULT_SPEC = importlib.util.spec_from_file_location("sync_result", RESULT)
RESULT_MODULE = importlib.util.module_from_spec(RESULT_SPEC)
RESULT_SPEC.loader.exec_module(RESULT_MODULE)
PR_DISPATCH_SPEC = importlib.util.spec_from_file_location(
    "pr_build_dispatch", ROOT / "scripts/pr-build-dispatch.py",
)
PR_DISPATCH_MODULE = importlib.util.module_from_spec(PR_DISPATCH_SPEC)
PR_DISPATCH_SPEC.loader.exec_module(PR_DISPATCH_MODULE)
IMPACT_SPEC = importlib.util.spec_from_file_location(
    "platform_impact", ROOT / "scripts/platform_impact.py",
)
IMPACT_MODULE = importlib.util.module_from_spec(IMPACT_SPEC)
IMPACT_SPEC.loader.exec_module(IMPACT_MODULE)
CONTROL_SPEC = importlib.util.spec_from_file_location(
    "upstream_sync_control", ROOT / "scripts/upstream_sync_control.py",
)
CONTROL_MODULE = importlib.util.module_from_spec(CONTROL_SPEC)
CONTROL_SPEC.loader.exec_module(CONTROL_MODULE)
SYNC_WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/sync-upstream.yml").read_text(), Loader=yaml.BaseLoader,
)


def workflow_step(workflow: dict, name: str) -> dict:
    return next(step for step in workflow["jobs"]["prepare_candidate"]["steps"]
                if step.get("name") == name)


def outputs_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


class HelperEntryTests(unittest.TestCase):
    def test_official_perl_helper_is_executable_and_sync_feeds_calls_its_entry(self):
        helper = ROOT / "scripts/sync-official-perl.sh"
        self.assertTrue(os.access(helper, os.X_OK), "the helper must remain executable")
        staged = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "--stage", "scripts/sync-official-perl.sh"],
            text=True,
        )
        self.assertIn("100755", staged)
        result = subprocess.run([str(helper), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--locked", result.stdout)

        transaction = (ROOT / "scripts/sync-feeds-transaction.py").read_text()
        self.assertIn('"./scripts/sync-official-perl.sh", "--locked"', transaction)


class GitFixture:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(exist_ok=True)
        self.env = os.environ.copy()
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Sync Test",
            "GIT_AUTHOR_EMAIL": "sync-test@example.invalid",
            "GIT_COMMITTER_NAME": "Sync Test",
            "GIT_COMMITTER_EMAIL": "sync-test@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        })
        self.git("init", "--quiet", "--initial-branch=master")

    def git(self, *args, input=None, check=True):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], cwd=self.root, env=self.env,
            input=input, text=True, capture_output=True, check=check,
        ).stdout.strip()

    def tree(self, files: dict[str, str], parent_tree: str | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="lede-test-index-") as temporary:
            index = Path(temporary) / "index"
            env = self.env | {"GIT_INDEX_FILE": str(index)}
            if parent_tree:
                subprocess.run(["git", "-C", str(self.root), "read-tree", parent_tree], env=env, check=True)
            else:
                subprocess.run(["git", "-C", str(self.root), "read-tree", "--empty"], env=env, check=True)
            for path, content in files.items():
                blob = self.git("hash-object", "-w", "--stdin", input=content)
                subprocess.run(
                    ["git", "-C", str(self.root), "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                    env=env, check=True,
                )
            return subprocess.check_output(["git", "-C", str(self.root), "write-tree"], env=env, text=True).strip()

    def commit(self, tree: str, *parents: str, message: str = "test commit") -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, input=message + "\n")


class UpstreamSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lede-upstream-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.repo = GitFixture(self.root)
        self.base_file = {
            "feeds.conf.default": "src-git packages https://example.invalid/packages.git\n",
            "patches/dockerd/100-skip-host-nested-binaries.patch": "diff --git a/a b/a\n",
            ".github/feed-lock.json": json.dumps({"schema_version": 1, "feeds": [], "overlays": {}}) + "\n",
            "README": "base\n",
        }
        self.origin = self.repo.commit(self.repo.tree(self.base_file), message="shared ancestor")
        self.base = self.make_base(self.origin)
        self.upstream = self.make_upstream(self.origin)
        self.workflow = self.base

    def make_base(self, parent: str, extra: dict[str, str] | None = None,
                  state_upstream: str | None = None, fork_base: str | None = None) -> str:
        files = dict(self.base_file)
        if extra:
            files.update(extra)
        state = {
            "schema_version": 1,
            "upstream_repository": "coolsnowwolf/lede",
            "upstream_branch": "master",
            "upstream_sha": state_upstream or self.origin,
            "fork_base_sha": fork_base or self.origin,
        }
        files[".github/upstream-state.json"] = json.dumps(state) + "\n"
        return self.repo.commit(self.repo.tree(files), parent, message="fork base")

    def make_upstream(self, parent: str, extra: dict[str, str] | None = None) -> str:
        files = {"upstream.txt": "upstream\n"}
        if extra:
            files.update(extra)
        return self.repo.commit(self.repo.tree(files), parent, message="upstream")

    def run_sync(self, command="report", *, base=None, upstream=None, candidate=None,
                 output=None, dry_run=True, extra=()):
        argv = [
            "python3", str(SYNC), command, "--repo", str(self.root),
            "--base", base or self.base, "--upstream", upstream or self.upstream,
            "--workflow-sha", self.workflow, "--sync-id", "upstream-test-identity",
        ]
        if output:
            argv.extend(["--output", str(output)])
        if candidate:
            argv.extend(["--candidate-ref", candidate])
        if dry_run:
            argv.append("--dry-run")
        argv.extend(extra)
        return subprocess.run(argv, cwd=self.root, env=self.repo.env, text=True, capture_output=True)

    def report(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_normal_candidate_has_two_parent_merge_and_idempotent_reuse(self):
        report = self.report(self.run_sync())
        self.assertEqual(report["status"], "candidate_ready")
        self.assertEqual(report["candidate_branch"],
                         f"sync/upstream-{self.base[:12]}-{self.upstream[:12]}-w{self.workflow[:12]}")
        candidate = report["candidate_sha"]
        merge_sha = report["merge_commit_sha"]
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", merge_sha).split(), [self.base, self.upstream])
        ancestor = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", self.base, candidate])
        self.assertEqual(ancestor.returncode, 0)
        self.assertEqual(json.loads(self.repo.git("show", f"{candidate}:.github/upstream-state.json")), {
            "schema_version": 1,
            "upstream_repository": "coolsnowwolf/lede",
            "upstream_branch": "master",
            "upstream_sha": self.upstream,
            "fork_base_sha": self.base,
        })

        ref = "refs/heads/candidate"
        self.repo.git("update-ref", ref, candidate)
        repeated = self.report(self.run_sync(candidate=ref))
        self.assertEqual(repeated["status"], "candidate_reused")
        self.assertEqual(repeated["candidate_sha"], candidate)
        self.assertFalse(repeated["candidate_modified"])
        self.assertEqual(repeated["feed_lock_sha256"], report["feed_lock_sha256"])

    def test_candidate_keeps_its_pinned_upstream_after_remote_upstream_advances(self):
        initial = self.report(self.run_sync("prepare", extra=("--no-lock-refresh",)))
        candidate_ref = "refs/heads/pinned-candidate"
        self.repo.git("update-ref", candidate_ref, initial["candidate_sha"])
        newer_upstream = self.make_upstream(self.upstream, {"newer.txt": "later upstream\n"})
        self.repo.git("update-ref", "refs/remotes/upstream/master", newer_upstream)

        selected = self.report(self.run_sync(
            "prepare", upstream=self.upstream, candidate=candidate_ref,
            extra=("--no-lock-refresh",),
        ))
        self.assertEqual(selected["status"], "candidate_reused")
        self.assertEqual(selected["upstream_sha"], self.upstream)
        self.assertNotEqual(selected["upstream_sha"], newer_upstream)
        self.assertEqual(selected["candidate_sha"], initial["candidate_sha"])

    def test_no_updates_when_upstream_is_already_in_base(self):
        files = dict(self.base_file)
        files[".github/upstream-state.json"] = json.dumps({
            "schema_version": 1,
            "upstream_repository": "coolsnowwolf/lede",
            "upstream_branch": "master",
            "upstream_sha": self.origin,
            "fork_base_sha": self.origin,
        }) + "\n"
        merged_base = self.repo.commit(self.repo.tree(files), self.base, self.upstream, message="already merged")
        result = self.report(self.run_sync(base=merged_base))
        self.assertEqual(result["status"], "no_updates")
        self.assertNotIn("candidate_sha", result)

    def test_emmc_conflict_report_includes_three_blob_ids(self):
        path = "target/linux/qualcommax/image/ipq60xx.mk"
        common = "define Device/EmmcImage\n\tIMAGES += factory.bin\nendef\n"
        fork = "define Device/EmmcImage\n\tIMAGES += factory.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        upstream = "define Device/EmmcImage\n\tIMAGES += factory.bin recovery.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        self.origin = self.repo.commit(self.repo.tree({**self.base_file, path: common}), message="common EmmcImage")
        self.base = self.make_base(self.origin, {path: fork})
        self.workflow = self.base
        self.upstream = self.make_upstream(self.origin, {path: upstream})
        output = Path(self.temp.name) / "conflict-report.json"
        result = self.run_sync(output=output)
        report = self.report(result)
        self.assertEqual(report["status"], "needs_resolution")
        self.assertEqual(report["conflicts"][0]["path"], path)
        self.assertTrue(all(report["conflicts"][0][key] for key in ("merge_base_blob", "base_blob", "upstream_blob")))
        self.assertEqual(json.loads(output.read_text())["status"], "needs_resolution")
        self.assertIn(self.base, report["reproduce"])
        self.assertIn(self.upstream, report["reproduce"])

    def test_upstream_history_rewrite_is_blocked(self):
        old_upstream = self.make_upstream(self.origin, {"upstream-old.txt": "old\n"})
        self.base = self.make_base(old_upstream, state_upstream=old_upstream, fork_base=self.origin)
        self.workflow = self.base
        rewritten = self.make_upstream(self.origin, {"upstream-new.txt": "rewritten\n"})
        output = Path(self.temp.name) / "rewrite-report.json"
        result = self.run_sync(upstream=rewritten, output=output)
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "history_review_required")
        self.assertIn("not an ancestor", report["error"])

    def test_multiple_merge_bases_are_blocked(self):
        a = self.repo.commit(self.repo.tree({"a.txt": "a\n"}, self.repo.git("rev-parse", f"{self.origin}^{{tree}}")), self.origin, message="side A")
        b = self.repo.commit(self.repo.tree({"b.txt": "b\n"}, self.repo.git("rev-parse", f"{self.origin}^{{tree}}")), self.origin, message="side B")
        combined = self.repo.tree({
            "feeds.conf.default": self.base_file["feeds.conf.default"],
            "patches/dockerd/100-skip-host-nested-binaries.patch": self.base_file["patches/dockerd/100-skip-host-nested-binaries.patch"],
            ".github/feed-lock.json": self.base_file[".github/feed-lock.json"],
            "README": "base\n", "a.txt": "a\n", "b.txt": "b\n",
            ".github/upstream-state.json": json.dumps({
                "schema_version": 1, "upstream_repository": "coolsnowwolf/lede",
                "upstream_branch": "master", "upstream_sha": self.origin,
                "fork_base_sha": self.origin,
            }) + "\n",
        })
        first = self.repo.commit(combined, a, b, message="merge A B")
        second = self.repo.commit(combined, b, a, message="merge B A")
        self.base = first
        self.upstream = second
        self.workflow = first
        result = self.run_sync(output=Path(self.temp.name) / "multi-base.json")
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((Path(self.temp.name) / "multi-base.json").read_text())
        self.assertEqual(report["status"], "history_review_required")
        self.assertIn("found 2", report["error"])

    def test_candidate_modification_is_detected_and_base_advance_rejects_old_candidate(self):
        initial = self.report(self.run_sync())
        candidate = initial["candidate_sha"]
        ref = "refs/heads/candidate"
        self.repo.git("update-ref", ref, candidate)
        extra_tree = self.repo.tree({"review-note.txt": "manual review\n"}, self.repo.git("rev-parse", f"{candidate}^{{tree}}"))
        modified = self.repo.commit(extra_tree, candidate, message="manual candidate review")
        self.repo.git("update-ref", ref, modified)
        reused = self.report(self.run_sync(candidate=ref))
        self.assertEqual(reused["candidate_sha"], modified)
        self.assertTrue(reused["candidate_modified"])

        advanced = self.make_base(self.base, {"later.txt": "master advanced\n"})
        stale = self.run_sync(base=advanced, candidate=ref, output=Path(self.temp.name) / "stale.json")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("identity mismatch", json.loads((Path(self.temp.name) / "stale.json").read_text())["error"])
        self.assertEqual(self.repo.git("rev-parse", ref), modified)

    def test_conflict_resolved_candidate_registration_is_idempotent(self):
        path = "target/linux/qualcommax/image/ipq60xx.mk"
        fork = "define Device/EmmcImage\n\tIMAGES += factory.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        upstream = "define Device/EmmcImage\n\tIMAGES += factory.bin recovery.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        self.base = self.make_base(self.origin, {path: fork})
        self.workflow = self.base
        self.upstream = self.make_upstream(self.origin, {path: upstream})
        resolved = "define Device/EmmcImage\n\tIMAGES += factory.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        state = {
            "schema_version": 1,
            "upstream_repository": "coolsnowwolf/lede",
            "upstream_branch": "master",
            "upstream_sha": self.origin,
            "fork_base_sha": self.origin,
        }
        merge = self.repo.commit(self.repo.tree({
            **self.base_file,
            ".github/upstream-state.json": json.dumps(state) + "\n",
            path: resolved,
            "upstream.txt": "upstream\n",
        }), self.base, self.upstream, message="resolve EmmcImage conflict")
        ref = "refs/heads/sync/upstream-test"
        self.repo.git("update-ref", ref, merge)
        result = subprocess.run([
            "python3", str(SYNC), "register", "--repo", str(self.root),
            "--base", self.base, "--upstream", self.upstream,
            "--workflow-sha", self.workflow, "--sync-id", "upstream-test-identity",
            "--candidate-ref", ref, "--dry-run",
            "--no-lock-refresh",
        ], cwd=self.root, env=self.repo.env, text=True, capture_output=True)
        report = self.report(result)
        self.assertEqual(report["status"], "candidate_registered")
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", merge).split(), [self.base, self.upstream])
        first_candidate = report["candidate_sha"]
        self.repo.git("update-ref", ref, first_candidate)
        repeated = subprocess.run([
            "python3", str(SYNC), "register", "--repo", str(self.root),
            "--base", self.base, "--upstream", self.upstream,
            "--workflow-sha", self.workflow, "--sync-id", "upstream-test-identity",
            "--candidate-ref", ref, "--dry-run",
            "--no-lock-refresh",
        ], cwd=self.root, env=self.repo.env, text=True, capture_output=True)
        self.assertEqual(self.report(repeated)["candidate_sha"], first_candidate)

        unresolved = self.repo.commit(self.repo.tree({
            **self.base_file,
            ".github/upstream-state.json": json.dumps(state) + "\n",
            path: "<<<<<<< HEAD\nfork\n=======\nupstream\n>>>>>>> upstream/master\n",
            "upstream.txt": "upstream\n",
        }), self.base, self.upstream, message="unresolved conflict")
        bad_ref = "refs/heads/unresolved"
        self.repo.git("update-ref", bad_ref, unresolved)
        rejected = subprocess.run([
            "python3", str(SYNC), "register", "--repo", str(self.root),
            "--base", self.base, "--upstream", self.upstream,
            "--workflow-sha", self.workflow, "--sync-id", "upstream-test-identity",
            "--candidate-ref", bad_ref, "--dry-run", "--no-lock-refresh",
        ], cwd=self.root, env=self.repo.env, text=True, capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unresolved conflict markers", rejected.stderr)


class SyncWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lede-sync-workflow-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.repo = GitFixture(self.root)
        self.lock = {"schema_version": 1, "feeds": [], "overlays": {}}
        self.base_file = {
            "feeds.conf.default": "src-git packages https://example.invalid/packages.git^" + "1" * 40 + "\n",
            "patches/dockerd/100-skip-host-nested-binaries.patch": "patch\n",
            ".github/feed-lock.json": json.dumps(self.lock) + "\n",
            "README": "fork\n",
        }
        self.origin_sha = self.repo.commit(self.repo.tree(self.base_file), message="shared base")
        self.base_sha = self.make_base(self.origin_sha)
        self.upstream_sha = self.make_upstream(self.origin_sha)
        self.workflow_sha = self.base_sha
        self.repo.git("update-ref", "refs/heads/master", self.base_sha)
        self.repo.git("checkout", "--force", "master")
        self.repo.git("update-ref", "refs/remotes/upstream/master", self.upstream_sha)

        self.remote = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=master", str(self.remote)], check=True)
        self.repo.git("remote", "add", "origin", str(self.remote))
        self.repo.git("push", "--quiet", "origin", f"{self.base_sha}:refs/heads/master")
        self.upstream_remote = Path(self.temp.name) / "upstream.git"
        subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=master", str(self.upstream_remote)], check=True)
        self.repo.git("push", "--quiet", str(self.upstream_remote), f"{self.upstream_sha}:refs/heads/master")

        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("upstream-sync.py", "upstream_sync_control.py", "platform_impact.py"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        # Keep the actual prepare helper in the chain while making its lock generator local.
        (scripts / "feed-lock.py").write_text(
            "#!/usr/bin/env python3\n"
            "import argparse, json, pathlib\n"
            "p=argparse.ArgumentParser(); p.add_argument('command'); p.add_argument('--root'); "
            "p.add_argument('--feeds-conf'); p.add_argument('--output'); p.add_argument('--print-digest', action='store_true')\n"
            f"lock={json.dumps(self.lock)}\n"
            "a=p.parse_args(); pathlib.Path(a.output).parent.mkdir(parents=True, exist_ok=True); "
            "open(a.output, 'w').write(json.dumps(lock) + '\\n')\n"
        )

        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.gh_log = Path(self.temp.name) / "gh-writes.jsonl"
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args=sys.argv[1:]\n"
            "if args[:2] == ['pr', 'list']:\n"
            " print(open(os.environ['MOCK_PULLS']).read())\n"
            "elif args[:1] == ['api']:\n"
            " payload=sys.stdin.read() if '--input' in args else ''\n"
            " with open(os.environ['GH_WRITE_LOG'], 'a') as f: f.write(json.dumps({'args': args, 'payload': payload})+'\\n')\n"
            "else: raise SystemExit('unexpected gh invocation: '+repr(args))\n"
        )
        gh.chmod(0o755)

    def make_base(self, parent: str, extra: dict[str, str] | None = None) -> str:
        files = dict(self.base_file)
        if extra:
            files.update(extra)
        state = {
            "schema_version": 1,
            "upstream_repository": "coolsnowwolf/lede",
            "upstream_branch": "master",
            "upstream_sha": self.origin_sha,
            "fork_base_sha": self.origin_sha,
        }
        files[".github/upstream-state.json"] = json.dumps(state) + "\n"
        return self.repo.commit(self.repo.tree(files), parent, message="fork base")

    def make_upstream(self, parent: str, extra: dict[str, str] | None = None) -> str:
        files = dict(self.base_file)
        files["upstream.txt"] = "upstream\n"
        if extra:
            files.update(extra)
        return self.repo.commit(self.repo.tree(files, self.repo.git("rev-parse", f"{parent}^{{tree}}")), parent,
                                message="upstream commit")

    @property
    def planned_branch(self):
        return CONTROL_MODULE.candidate_branch(self.base_sha, self.upstream_sha, self.workflow_sha)

    @property
    def sync_id(self):
        return CONTROL_MODULE.candidate_sync_id(self.base_sha, self.upstream_sha, self.workflow_sha)

    def run_ref_discovery(self):
        step = workflow_step(SYNC_WORKFLOW, "Fetch and pin fork and upstream commits")
        run = step["run"].replace(
            "https://github.com/coolsnowwolf/lede.git", str(self.upstream_remote),
        )
        if "upstream" in self.repo.git("remote").splitlines():
            self.repo.git("remote", "remove", "upstream")
        output = Path(self.temp.name) / "refs.out"
        output.write_text("")
        environment = self.repo.env | {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "DEFAULT_BRANCH": "master", "WORKFLOW_SHA": self.workflow_sha,
            "GITHUB_OUTPUT": str(output),
        }
        result = subprocess.run(
            ["bash", "-e", "-u", "-o", "pipefail", "-c", run],
            cwd=self.root, env=environment, text=True, capture_output=True,
        )
        return result, outputs_file(output)

    def run_discovery(self, *, mode="prepare", dry_run="true", current_base=None,
                      upstream=None, requested_ref="", pulls=None):
        step = workflow_step(SYNC_WORKFLOW, "Find an existing candidate without changing it")
        run = step["run"]
        refs_result, refs = self.run_ref_discovery()
        if refs_result.returncode:
            return refs_result, {}, refs
        if current_base:
            self.assertEqual(refs["base_sha"], current_base)
        if upstream:
            self.assertEqual(refs["upstream_sha"], upstream)
        for key, value in refs.items():
            run = run.replace(f"${{{{ steps.refs.outputs.{key} }}}}", value)
        pulls_path = Path(self.temp.name) / "pulls.json"
        pulls_path.write_text(json.dumps(pulls or []))
        output = Path(self.temp.name) / f"discovery-{mode}-{dry_run}.out"
        output.write_text("")
        environment = self.repo.env | {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "DEFAULT_BRANCH": "master", "MODE": mode, "DRY_RUN": dry_run,
            "REQUESTED_CANDIDATE_REF": requested_ref,
            "WORKFLOW_SHA": self.workflow_sha,
            "GITHUB_REPOSITORY": "example/lede", "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(output), "MOCK_PULLS": str(pulls_path),
            "GH_WRITE_LOG": str(self.gh_log),
        }
        result = subprocess.run(
            ["bash", "-e", "-u", "-o", "pipefail", "-c", run],
            cwd=self.root, env=environment, text=True, capture_output=True,
        )
        return result, outputs_file(output), refs

    def run_prepare_step(self, existing: dict[str, str], refs: dict[str, str], *, mode="prepare", dry_run="true"):
        step = workflow_step(SYNC_WORKFLOW, "Prepare a report or a candidate commit")
        output = Path(self.temp.name) / f"prepare-{mode}-{dry_run}.out"
        output.write_text("")
        environment = self.repo.env | {
            "BASE_SHA": existing["base_sha"],
            "UPSTREAM_SHA": existing["upstream_sha"],
            "PINNED_WORKFLOW_SHA": existing["workflow_sha"],
            "SYNC_ID": existing["sync_id"],
            "EXISTING_CANDIDATE": existing["candidate_ref"],
            "MODE": mode, "DRY_RUN": dry_run,
            "GITHUB_OUTPUT": str(output),
        }
        result = subprocess.run(
            ["bash", "-e", "-u", "-o", "pipefail", "-c", step["run"]],
            cwd=self.root, env=environment, text=True, capture_output=True,
        )
        report_path = self.root / "sync-report.json"
        return result, outputs_file(output), json.loads(report_path.read_text()) if report_path.exists() else None

    def prepare_without_candidate(self, *, mode="prepare"):
        found, existing, refs = self.run_discovery(mode=mode)
        self.assertEqual(found.returncode, 0, found.stdout + found.stderr)
        prepared = self.run_prepare_step(existing, refs, mode=mode)
        self.assertEqual(prepared[0].returncode, 0, prepared[0].stdout + prepared[0].stderr)
        return found, existing, refs, prepared

    def test_actual_discovery_hands_only_existing_ref_to_prepare_for_clean_and_conflicted_merges(self):
        found, existing, _, prepared = self.prepare_without_candidate()
        self.assertEqual(existing["candidate_branch"], self.planned_branch)
        self.assertEqual(existing["exists"], "false")
        self.assertEqual(existing["candidate_ref"], "")
        self.assertEqual(prepared[2]["status"], "candidate_ready")
        self.assertEqual(prepared[2]["candidate_branch"], self.planned_branch)

        # Recreate the old discovery output and prove that the real helper rejects
        # its non-existent planned ref instead of treating it as an existing candidate.
        old_outputs = dict(existing, candidate_ref=f"refs/heads/{self.planned_branch}")
        old_path = self.run_prepare_step(old_outputs, {}, dry_run="true")
        self.assertNotEqual(old_path[0].returncode, 0)
        self.assertIn("existing candidate does not resolve to a commit", old_path[0].stderr)

        conflict_path = "target/linux/qualcommax/image/ipq60xx.mk"
        base_content = "define Device/EmmcImage\n\tIMAGES += factory.bin\nendef\n"
        fork_content = "define Device/EmmcImage\n\tIMAGES += factory.bin\n\tIMAGE/factory.bin := pad-to 6144k\nendef\n"
        upstream_content = "define Device/EmmcImage\n\tIMAGES += factory.bin recovery.bin\nendef\n"
        self.origin_sha = self.repo.commit(self.repo.tree({**self.base_file, conflict_path: base_content}),
                                           message="shared conflict base")
        self.base_sha = self.make_base(self.origin_sha, {conflict_path: fork_content})
        self.upstream_sha = self.make_upstream(self.origin_sha, {conflict_path: upstream_content})
        self.workflow_sha = self.base_sha
        self.repo.git("update-ref", "refs/heads/master", self.base_sha)
        self.repo.git("checkout", "--force", "master")
        self.repo.git("update-ref", "refs/remotes/upstream/master", self.upstream_sha)
        self.repo.git("push", "--quiet", "--force", "origin", f"{self.base_sha}:refs/heads/master")
        self.repo.git("push", "--quiet", "--force", str(self.upstream_remote), f"{self.upstream_sha}:refs/heads/master")
        conflicted = self.prepare_without_candidate()
        self.assertEqual(conflicted[3][2]["status"], "needs_resolution")
        self.assertEqual(conflicted[3][2]["conflicts"][0]["path"], conflict_path)

    def test_existing_candidate_identity_is_discovered_and_reused_and_register_requires_it(self):
        _, _, _, first = self.prepare_without_candidate()
        initial = first[2]
        branch = initial["candidate_branch"]
        self.repo.git("push", "--quiet", "origin", f"{initial['candidate_sha']}:refs/heads/{branch}")
        pull = {
            "number": 12, "headRefName": branch, "headRefOid": initial["candidate_sha"],
            "headRepository": {"nameWithOwner": "example/lede"},
            "baseRefName": "master", "baseRefOid": self.base_sha, "url": "https://example.invalid/pr/12",
        }
        discovered, existing, refs = self.run_discovery(pulls=[pull])
        self.assertEqual(discovered.returncode, 0, discovered.stdout + discovered.stderr)
        self.assertEqual(existing["candidate_branch"], branch)
        self.assertEqual(existing["candidate_ref"], f"refs/remotes/origin/{branch}")
        self.assertEqual(existing["base_sha"], self.base_sha)
        self.assertEqual(existing["upstream_sha"], self.upstream_sha)
        self.assertEqual(existing["workflow_sha"], self.workflow_sha)
        reused = self.run_prepare_step(existing, refs)
        self.assertEqual(reused[0].returncode, 0, reused[0].stdout + reused[0].stderr)
        self.assertEqual(reused[2]["status"], "candidate_reused")
        self.assertEqual(reused[2]["candidate_sha"], initial["candidate_sha"])

        self.repo.git("push", "--quiet", "origin", "--delete", branch)
        missing = self.run_discovery(mode="register", pulls=[])
        self.assertNotEqual(missing[0].returncode, 0)
        self.assertIn("register mode candidate branch does not exist", missing[0].stderr)

    def test_expired_candidate_reports_locally_and_only_non_dry_run_clears_remote_ready_status(self):
        _, _, _, first = self.prepare_without_candidate()
        candidate = first[2]
        branch = candidate["candidate_branch"]
        self.repo.git("push", "--quiet", "origin", f"{candidate['candidate_sha']}:refs/heads/{branch}")
        pull = {"number": 12, "headRefName": branch, "headRefOid": candidate["candidate_sha"],
                "headRepository": {"nameWithOwner": "example/lede"}, "baseRefName": "master"}
        advanced = self.make_base(self.base_sha, {"README": "default branch moved\n"})
        self.repo.git("push", "--quiet", "origin", f"{advanced}:refs/heads/master")

        local_step = workflow_step(SYNC_WORKFLOW, "Report a candidate made stale by default-branch movement")
        status_step = workflow_step(SYNC_WORKFLOW, "Clear ready status for an expired candidate")
        self.assertIn("inputs.mode != 'report'", status_step["if"])
        self.assertIn("inputs.dry_run != true", status_step["if"])

        for mode, dry_run, should_write in (("report", "false", False),
                                            ("prepare", "true", False),
                                            ("prepare", "false", True)):
            with self.subTest(mode=mode, dry_run=dry_run):
                self.gh_log.unlink(missing_ok=True)
                discovery, existing, refs = self.run_discovery(
                    mode=mode, dry_run=dry_run, current_base=advanced, pulls=[pull],
                )
                self.assertEqual(discovery.returncode, 0, discovery.stdout + discovery.stderr)
                self.assertEqual(existing["stale"], "true")
                self.assertEqual(existing["candidate_ref"], f"refs/remotes/origin/{branch}")
                candidate_ref = existing["candidate_ref"]
                candidate_sha = self.repo.git("rev-parse", f"{candidate_ref}^{{commit}}")
                report = Path(self.temp.name) / f"stale-{mode}-{dry_run}.json"
                env = self.repo.env | {
                    "CURRENT_BASE_SHA": advanced,
                    "PINNED_BASE_SHA": existing["base_sha"],
                    "PINNED_UPSTREAM_SHA": existing["upstream_sha"],
                    "PINNED_WORKFLOW_SHA": existing["workflow_sha"],
                    "CANDIDATE_BRANCH": existing["candidate_branch"],
                    "CANDIDATE_REF": candidate_ref,
                }
                result = subprocess.run(["bash", "-e", "-u", "-o", "pipefail", "-c", local_step["run"]],
                                        cwd=self.root, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                stale_report = json.loads((self.root / "sync-report.json").read_text())
                self.assertEqual(stale_report["status"], "candidate_expired")
                self.assertEqual(stale_report["candidate_sha"], candidate_sha)

                condition_allows_write = mode != "report" and dry_run != "true"
                self.assertEqual(condition_allows_write, should_write)
                write_env = env | {
                    "MODE": mode, "DRY_RUN": dry_run,
                    "CANDIDATE_REF": candidate_ref,
                    "GITHUB_REPOSITORY": "example/lede", "GH_WRITE_LOG": str(self.gh_log),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                }
                write = subprocess.run(["bash", "-e", "-u", "-o", "pipefail", "-c", status_step["run"]],
                                       cwd=self.root, env=write_env, text=True, capture_output=True)
                if condition_allows_write:
                    self.assertEqual(write.returncode, 0, write.stdout + write.stderr)
                    records = [json.loads(line) for line in self.gh_log.read_text().splitlines()]
                    self.assertEqual(len(records), 1)
                    self.assertIn(f"repos/example/lede/statuses/{candidate_sha}", records[0]["args"])
                    self.assertEqual(json.loads(records[0]["payload"])["state"], "failure")
                else:
                    self.assertNotEqual(write.returncode, 0)
                    self.assertIn("refusing remote status writes", write.stderr)
                    self.assertFalse(self.gh_log.exists())

    def make_candidate_aggregation_fixture(self):
        _, _, _, prepared = self.prepare_without_candidate()
        report = prepared[2]
        candidate_sha = report["candidate_sha"]
        manifest = json.loads(self.repo.git("show", f"{candidate_sha}:.github/upstream-candidate.json"))
        lock = json.loads(self.repo.git("show", f"{candidate_sha}:.github/feed-lock.json"))
        branch = report["candidate_branch"]
        sync_id = manifest["sync_id"]
        metadata = {
            "run_id": "123", "run_attempt": "1", "workflow_sha": self.workflow_sha,
            "source_sha": candidate_sha, "sync_id": sync_id,
            "feed_lock_sha256": RESULT_MODULE.canonical_digest(lock),
            "required_jobs": ["validate_source", "build_openwrt"],
            "workflow_file": ".github/workflows/openwrt-ci.yml",
            "profile": "x86_64-passwall-docker", "repository": "example/lede",
            "dispatch_sha": self.workflow_sha,
            "platform_impact": {"schema_version": 1, "platforms": {}},
            "feed_revisions": [], "event": "workflow_dispatch",
            "validation": {"source_sha_match": True, "feed_lock_match": True,
                           "candidate_identity_match": True, "feed_revisions_collected": True},
            "candidate": dict(manifest),
        }
        run = {
            "id": 123, "run_attempt": 1, "workflow_id": 77, "status": "completed",
            "conclusion": "success", "event": "workflow_dispatch",
            "head_branch": CONTROL_MODULE.candidate_control_ref(self.workflow_sha),
            "head_sha": self.workflow_sha,
            "display_title": f"OpenWrt {sync_id} {candidate_sha}",
            "path": ".github/workflows/openwrt-ci.yml@refs/tags/ci/sync-control-test",
            "html_url": "https://example.invalid/runs/123",
        }
        pull = {
            "number": 12, "state": "open", "draft": False,
            "html_url": "https://example.invalid/pr/12",
            "head": {"ref": branch, "sha": candidate_sha,
                     "repo": {"full_name": "example/lede"}},
            "base": {"ref": "master", "sha": self.base_sha},
            "user": {"login": "candidate-author"},
        }
        artifacts = [
            {"id": 201, "name": f"openwrt-x86_64-passwall-docker-{sync_id}-validation-123-1", "expired": False},
            {"id": 202, "name": f"openwrt-x86_64-passwall-docker-{sync_id}-123-1", "expired": False},
        ]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sync-validation.json", json.dumps(metadata))
            bundle.writestr("feed-lock.json", json.dumps(lock))
        return candidate_sha, manifest, metadata, run, pull, artifacts, archive.getvalue()

    def run_aggregation(self, trigger, *, pull_items=None, changed_paths=(), run_override=None):
        candidate_sha, manifest, metadata, run, pull, artifacts, artifact_bytes = self.make_candidate_aggregation_fixture()
        if run_override:
            run = run_override(run)
        plan = IMPACT_MODULE.plan_for_paths(list(changed_paths))
        metadata["platform_impact"] = plan
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sync-validation.json", json.dumps(metadata))
            bundle.writestr("feed-lock.json", json.dumps(json.loads(self.repo.git("show", f"{candidate_sha}:.github/feed-lock.json"))))
        artifact_bytes = archive.getvalue()
        if trigger == "workflow_run":
            pull_items = pull_items if pull_items is not None else [pull]
        event = {
            "repository": {"default_branch": "master"},
            "workflow_run": {"id": 123, "run_attempt": 1, "head_sha": self.workflow_sha},
            "issue": {"number": 12, "pull_request": {"url": "https://api.example/pulls/12"}},
        }
        event_path = Path(self.temp.name) / f"event-{trigger}.json"
        event_path.write_text(json.dumps(event))
        api_calls = []
        statuses = []

        def api(path, *flags):
            api_calls.append(path)
            if path == "repos/example/lede/actions/workflows/openwrt-ci.yml":
                return {"id": 77}
            if path == "repos/example/lede/actions/runs/123":
                return run
            if path.startswith("repos/example/lede/pulls?state=all&head="):
                return pull_items
            if path == "repos/example/lede/pulls/12":
                return pull
            if path.startswith("repos/example/lede/contents/.github/upstream-candidate.json?"):
                return {"content": __import__("base64").b64encode(json.dumps(manifest).encode()).decode()}
            if path == "repos/example/lede/actions/runs/123/attempts/1/jobs":
                return [{"jobs": [{"name": "validate_source", "conclusion": "success"},
                                   {"name": "build_openwrt", "conclusion": "success"}]}]
            if path == "repos/example/lede/branches/master":
                return {"commit": {"sha": self.base_sha}}
            if path == "repos/example/lede/actions/runs/123/artifacts?per_page=100":
                return {"artifacts": artifacts}
            if "/actions/workflows/77/runs?event=workflow_dispatch" in path:
                return [{"workflow_runs": [run]}]
            raise AssertionError(f"unexpected mocked GitHub API path: {path} {flags!r}")

        with tempfile.TemporaryDirectory(prefix="lede-aggregate-gh-") as temporary:
            gh_bin = Path(temporary) / "gh"
            gh_bin.write_text("#!/bin/sh\nexit 1\n")
            gh_bin.chmod(0o755)
            with mock.patch.dict(os.environ, {
                "GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY": "example/lede",
                "GITHUB_EVENT_NAME": trigger, "PATH": str(gh_bin.parent) + os.pathsep + os.environ.get("PATH", ""),
            }), mock.patch.object(RESULT_MODULE, "gh_json", side_effect=api), \
                    mock.patch.object(RESULT_MODULE, "compare_paths", return_value=list(changed_paths)), \
                    mock.patch.object(RESULT_MODULE, "candidate_merge_chain", return_value=True), \
                    mock.patch.object(RESULT_MODULE, "review_evidence", return_value=([], {})), \
                    mock.patch.object(RESULT_MODULE, "post_status", side_effect=lambda *args, **kwargs: statuses.append(args)), \
                    mock.patch.object(RESULT_MODULE.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=artifact_bytes, stderr=b"",
                    )):
                result = RESULT_MODULE.run_gh()
        return result, api_calls, statuses, pull, manifest, run

    def test_workflow_run_aggregator_uses_controller_branch_and_exact_run_name_head_query(self):
        result, api_calls, _, pull, manifest, run = self.run_aggregation("workflow_run")
        self.assertEqual(result["state"], "success", result["errors"])
        query = next(path for path in api_calls if "/pulls?state=all&head=" in path)
        head = parse_qs(query.split("?", 1)[1])["head"][0]
        self.assertEqual(head, f"example:{pull['head']['ref']}")
        self.assertEqual(pull["head"]["ref"], CONTROL_MODULE.candidate_branch(
            manifest["base_sha"], manifest["upstream_sha"], manifest["workflow_sha"],
        ))
        self.assertEqual(run["display_title"], f"OpenWrt {manifest['sync_id']} {pull['head']['sha']}")

        for items in (
            [dict(pull, head={**pull["head"], "repo": {"full_name": "fork/lede"}})],
            [dict(pull, head={**pull["head"], "sha": pull["head"]["sha"][:12]})],
            [pull, dict(pull, number=13)],
        ):
            with self.subTest(items=len(items), repository=items[0]["head"]["repo"]["full_name"]):
                with self.assertRaises(RESULT_MODULE.ResultError):
                    self.run_aggregation("workflow_run", pull_items=items)

    def test_issue_comment_uses_same_candidate_identity_and_missing_platform_evidence_stays_pending(self):
        result, api_calls, statuses, pull, manifest, run = self.run_aggregation(
            "issue_comment", changed_paths=["target/linux/qualcommax/image/ipq60xx.mk"],
        )
        self.assertEqual(result["state"], "pending", result["errors"])
        self.assertTrue(any("platform evidence pending" in error for error in result["errors"]))
        self.assertEqual(result["sync_id"], manifest["sync_id"])
        self.assertEqual(pull["head"]["ref"], CONTROL_MODULE.candidate_branch(
            manifest["base_sha"], manifest["upstream_sha"], manifest["workflow_sha"],
        ))
        self.assertEqual(run["display_title"], f"OpenWrt {manifest['sync_id']} {pull['head']['sha']}")
        self.assertEqual([status[2] for status in statuses], ["pending", "pending"])
        self.assertTrue(any("actions/workflows/77/runs?event=workflow_dispatch" in path for path in api_calls))


class UpstreamCandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.base = "1" * 40
        self.old_upstream = "2" * 40
        self.workflow = "3" * 40
        self.sync_id = f"upstream-{self.base[:12]}-{self.old_upstream[:12]}-w{self.workflow[:12]}"
        self.branch = f"sync/{self.sync_id}"
        self.manifest = {
            "base_sha": self.base, "upstream_sha": self.old_upstream,
            "workflow_sha": self.workflow, "sync_id": self.sync_id,
        }

    def test_open_candidate_reuses_full_pinned_identity_while_upstream_moves(self):
        pull = {"number": 7, "headRefName": self.branch, "baseRefName": "master",
                "headRepository": {"nameWithOwner": "example/lede"}}
        fork_candidate = dict(pull, number=8,
                              headRepository={"nameWithOwner": "contributor/lede"})
        selected = CONTROL_MODULE.select_open_candidate(
            [fork_candidate, pull], "master", "example/lede",
        )
        CONTROL_MODULE.validate_candidate_manifest(self.branch, self.manifest)
        latest_upstream = "4" * 40
        self.assertIs(selected, pull)
        self.assertEqual(self.manifest["upstream_sha"], self.old_upstream)
        self.assertNotEqual(self.manifest["upstream_sha"], latest_upstream)
        self.assertFalse(CONTROL_MODULE.candidate_expired(self.manifest, self.base))
        with self.assertRaisesRegex(CONTROL_MODULE.CandidateSelectionError, "multiple open"):
            CONTROL_MODULE.select_open_candidate([pull, dict(pull, number=9)], "master", "example/lede")

    def test_multiple_open_candidates_are_an_error_and_base_advance_expires_old_identity(self):
        first = {"headRefName": self.branch, "baseRefName": "master"}
        second_branch = "sync/upstream-aaaaaaaaaaaa-bbbbbbbbbbbb-wcccccccccccc"
        second = {"headRefName": second_branch, "baseRefName": "master"}
        with self.assertRaisesRegex(CONTROL_MODULE.CandidateSelectionError, "multiple open"):
            CONTROL_MODULE.select_open_candidate([first, second], "master")
        self.assertTrue(CONTROL_MODULE.candidate_expired(self.manifest, "5" * 40))


class FeedLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lede-feed-lock-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tree"
        self.root.mkdir()
        (self.root / ".github").mkdir()
        (self.root / "feeds/packages/lang/perl").mkdir(parents=True)
        (self.root / "feeds/packages/utils/dockerd/patches").mkdir(parents=True)
        (self.root / "feeds/luci").mkdir(parents=True)
        (self.root / "patches/dockerd").mkdir(parents=True)
        (self.root / "feeds.conf.default").write_text(
            "src-git packages https://example.invalid/packages.git\n"
            "src-git luci https://example.invalid/luci.git\n"
        )
        self.repo = GitFixture(self.root / "feeds/packages")
        base_files = {
            "lang/perl/Makefile": "PERL\n",
            "lang/perl/perlver.mk": "VERSION:=5\n",
            "utils/dockerd/Makefile": "PKG_NAME:=dockerd\n",
        }
        for path, content in base_files.items():
            target = self.repo.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.repo.git("add", ".")
        self.repo.git("commit", "--quiet", "-m", "feed commit")
        self.revision = self.repo.git("rev-parse", "HEAD")
        self.luci_repo = GitFixture(self.root / "feeds/luci")
        (self.luci_repo.root / "Makefile").write_text("LUCI_FEED:=locked\n")
        self.luci_repo.git("add", ".")
        self.luci_repo.git("commit", "--quiet", "-m", "luci feed commit")
        self.luci_revision = self.luci_repo.git("rev-parse", "HEAD")
        self.perl = self.root / "feeds/packages/lang/perl"
        (self.perl / "Makefile").write_text("OVERLAID\n")
        (self.perl / "perlver.mk").write_text("VERSION:=5\n")
        self.patch = b"dockerd patch\n"
        (self.root / "patches/dockerd/100-skip-host-nested-binaries.patch").write_bytes(self.patch)
        (self.root / "feeds/packages/utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch").write_bytes(self.patch)
        self.config = self.root / "feeds.conf.default"
        self.lock_path = self.root / ".github/feed-lock.json"
        self.lock = {
            "schema_version": 1,
            "feeds": [
                {"name": "packages", "url": "https://example.invalid/packages.git", "branch": "master", "configured_branch": None, "revision": self.revision, "order": 1},
                {"name": "luci", "url": "https://example.invalid/luci.git", "branch": "master", "configured_branch": None, "revision": self.luci_revision, "order": 2},
            ],
            "overlays": {
                "official_perl": {"repository": "https://example.invalid/perl.git", "branch": "main", "revision": "a" * 40, "path": "feeds/packages/lang/perl", "tree": "b" * 40, "sha256": self.digest_overlay()},
                "dockerd_patch": {"path": "patches/dockerd/100-skip-host-nested-binaries.patch", "staged_path": "feeds/packages/utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch", "sha256": hashlib.sha256(self.patch).hexdigest()},
            },
        }
        self.lock_path.write_text(json.dumps(self.lock, indent=2) + "\n")

    def digest_overlay(self):
        result = subprocess.run(["python3", str(FEED_LOCK), "overlay-digest", "--path", str(self.root / "feeds/packages/lang/perl")], capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def verify(self):
        return subprocess.run([
            "python3", str(FEED_LOCK), "verify", "--root", str(self.root),
            "--lock", str(self.lock_path), "--feeds-conf", str(self.config),
        ], capture_output=True, text=True)

    def test_locked_feed_and_overlays_pass_then_detect_revision_and_content_drift(self):
        good = self.verify()
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn("match lock", good.stdout)
        (self.perl / "Makefile").write_text("changed\n")
        changed_overlay = self.verify()
        self.assertNotEqual(changed_overlay.returncode, 0)
        self.assertIn("Perl overlay digest drift", changed_overlay.stderr)
        (self.perl / "Makefile").write_text("OVERLAID\n")

        (self.repo.root / "another.txt").write_text("next revision\n")
        self.repo.git("add", ".")
        self.repo.git("commit", "--quiet", "-m", "feed advanced")
        changed_feed = self.verify()
        self.assertNotEqual(changed_feed.returncode, 0)
        self.assertIn("feed revision drift: packages", changed_feed.stderr)

    def test_every_feed_rejects_staged_unstaged_and_untracked_worktree_changes(self):
        for kind in ("staged", "unstaged", "untracked"):
            with self.subTest(kind=kind):
                self.luci_repo.git("reset", "--hard", "HEAD")
                self.luci_repo.git("clean", "-fd")
                tracked = self.luci_repo.root / "Makefile"
                if kind == "untracked":
                    (self.luci_repo.root / "local.mk").write_text("local\n")
                else:
                    tracked.write_text("modified\n")
                    if kind == "staged":
                        self.luci_repo.git("add", "Makefile")
                result = self.verify()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unexpected feed working tree modification: luci:", result.stderr)

    def test_packages_overlay_exceptions_still_require_exact_locked_digest(self):
        self.assertEqual(self.verify().returncode, 0)
        (self.perl / "Makefile").write_text("changed Perl overlay\n")
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Perl overlay digest drift", result.stderr)


class ForkContractTests(unittest.TestCase):
    def test_current_source_contract_accepts_trusted_dispatch_builds(self):
        result = CONTRACT_MODULE.Contract(ROOT, None, "source").run()
        self.assertTrue(result["success"], result["errors"])

    def test_artifact_checks_are_required_before_archiving_and_release_writes(self):
        path = ".github/workflows/openwrt-ci.yml"
        original = (ROOT / path).read_text()
        mutations = (
            original.replace(
                '          python3 "$RUNNER_TEMP/check-build-artifacts.py" \\\n'
                '            --root "$GITHUB_WORKSPACE/bin" --contract "$RUNNER_TEMP/fork-contract.yml" \\\n'
                '            --output "$RUNNER_TEMP/artifact-package-check.json"\n',
                "",
                1,
            ),
            original.replace(
                '      - name: Recheck packaged manifests and IPK collection\n'
                '        run: |\n'
                '          set -euo pipefail\n'
                '          sudo apt-get update\n'
                '          sudo apt-get install -y python3-yaml\n'
                '          python3 .trusted-controller/scripts/check-build-artifacts.py \\\n'
                '            --release-assets release-assets \\\n'
                '            --contract .trusted-controller/.github/fork-contract.yml\n',
                "",
                1,
            ),
        )
        for mutated in mutations:
            with self.subTest(check_removed=mutated != original):
                checker = CONTRACT_MODULE.Contract(ROOT, None, "source")
                read = checker.path_text
                with mock.patch.object(
                    checker, "path_text",
                    side_effect=lambda relative, **kwargs: (
                        mutated if relative == path and not kwargs.get("trusted")
                        else read(relative, **kwargs)
                    ),
                ):
                    checker.check_provenance()
                self.assertEqual(checker.checks["source_provenance"]["result"], "failure")

    def test_provenance_rejects_direct_pr_build_triggers(self):
        path = ".github/workflows/openwrt-ci.yml"
        original = (ROOT / path).read_text()
        for trigger in ("pull_request", "pull_request_target"):
            with self.subTest(trigger=trigger):
                checker = CONTRACT_MODULE.Contract(ROOT, None, "source")
                read = checker.path_text
                modified = original.replace("on:\n", f"on:\n  {trigger}:\n", 1)
                with mock.patch.object(
                    checker, "path_text",
                    side_effect=lambda relative, **kwargs: (
                        modified if relative == path and not kwargs.get("trusted")
                        else read(relative, **kwargs)
                    ),
                ):
                    checker.check_provenance()
                self.assertEqual(checker.checks["source_provenance"]["result"], "failure")

    def test_lost_profile_selection_fails_and_required_paths_use_candidate_tree(self):
        profile = ROOT / "profiles/x86_64-passwall-docker.diffconfig"
        content = profile.read_text()
        self.assertIn("CONFIG_TARGET_x86_64=y", content)
        with tempfile.TemporaryDirectory(prefix="lede-contract-config-") as temporary:
            config = Path(temporary) / ".config"
            config.write_text(content.replace("CONFIG_TARGET_x86_64=y\n", ""))
            checker = CONTRACT_MODULE.Contract(ROOT, config, "source")
            result = checker.run()
        self.assertFalse(result["success"])
        self.assertTrue(any("CONFIG_TARGET_x86_64" in error for error in result["errors"]))

        with tempfile.TemporaryDirectory(prefix="lede-contract-forbidden-") as temporary:
            config = Path(temporary) / ".config"
            config.write_text(content + "CONFIG_PACKAGE_trojan-plus=y\n")
            checker = CONTRACT_MODULE.Contract(ROOT, config, "source")
            result = checker.run()
        self.assertFalse(result["success"])
        self.assertTrue(any("trojan-plus" in error for error in result["errors"]))

        with tempfile.TemporaryDirectory(prefix="lede-contract-source-tree-") as temporary:
            root = Path(temporary) / "repo"
            repo = GitFixture(root)
            commit = repo.commit(repo.tree({"README": "candidate\n"}))
            (root / ".github").mkdir()
            (root / ".github/fork-contract.yml").write_text("schema_version: 1\n")
            candidate_checker = object.__new__(CONTRACT_MODULE.Contract)
            candidate_checker.root = root
            candidate_checker.source_ref = commit
            self.assertTrue((root / ".github/fork-contract.yml").is_file())
            self.assertFalse(candidate_checker.source_path_exists(".github/fork-contract.yml"))

    def test_candidate_profile_and_generated_config_cannot_drop_required_features_together(self):
        with tempfile.TemporaryDirectory(prefix="lede-contract-mutual-drop-") as temporary:
            root = Path(temporary) / "repo"
            repo = GitFixture(root)
            profile_path = "profiles/x86_64-passwall-docker.diffconfig"
            contract = (ROOT / ".github/fork-contract.yml").read_text()
            profile = (ROOT / profile_path).read_text()
            base_tree = repo.tree({
                ".github/fork-contract.yml": contract,
                profile_path: profile,
            })
            trusted = repo.commit(base_tree, message="trusted contract")
            config = Path(temporary) / ".config"

            for mutated in (
                profile.replace("CONFIG_PACKAGE_docker=y\n", ""),
                "\n".join(profile.splitlines()[:3]) + "\n",
            ):
                with self.subTest(remaining_lines=len(mutated.splitlines())):
                    candidate_tree = repo.tree({profile_path: mutated}, base_tree)
                    candidate = repo.commit(candidate_tree, trusted, message="mutated profile")
                    config.write_text(mutated)
                    checker = CONTRACT_MODULE.Contract(
                        root, config, "source", source_ref=candidate, trusted_ref=trusted,
                    )
                    checker.check_profile()
                    result = checker.checks["x86_profile"]
                    self.assertEqual(result["result"], "failure")
                    self.assertIn("requires", result["detail"])


class ActionsControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lede-actions-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sha = "1" * 40
        self.source = "2" * 40
        self.base = "3" * 40
        self.upstream = "4" * 40
        self.merge_base = "5" * 40
        self.sync_id = "upstream-333333333333-444444444444-w555555555555"
        self.lock = {"schema_version": 1, "feeds": [], "overlays": {}}
        self.lock_sha = hashlib.sha256(json.dumps(self.lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def fake_gh(self, attempt=1, head_sha=None, missing_run_id=False,
                existing_runs=None, ready=False, expired=False):
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        executable = binary / "gh"
        script = f'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[2:]
path = [item for item in args if item.startswith('repos/')][-1]
if path.endswith('/actions/workflows/openwrt-ci.yml'):
    print(json.dumps({{'id': 77, 'path': '.github/workflows/openwrt-ci.yml', 'state': 'active'}}))
elif '/actions/workflows/77/runs?' in path:
    print(json.dumps([{{'total_count': len({existing_runs or []}), 'workflow_runs': {existing_runs or []}}}]))
elif '/commits/{self.source}/statuses?' in path:
    status = [{{'context': 'upstream/ready', 'state': 'success', 'target_url': 'https://example/runs/123'}}] if {ready!r} else []
    print(json.dumps(status))
elif '/actions/runs/123/artifacts?' in path:
    validation = {{'name': 'openwrt-x86_64-passwall-docker-{self.sync_id}-validation-123-1', 'expired': {expired!r}}}
    build = {{'name': 'openwrt-x86_64-passwall-docker-{self.sync_id}-123-1', 'expired': {expired!r}}}
    print(json.dumps({{'artifacts': [validation, build]}}))
elif path.endswith('/statuses/{self.source}'):
    payload = json.load(sys.stdin)
    open({str(self.root / 'pending-status.json')!r}, 'w').write(json.dumps(payload))
    print(json.dumps(payload))
elif '/git/ref/tags/' in path:
    print(json.dumps({{'message': 'Not Found'}}), file=sys.stderr)
    sys.exit(1)
elif '/git/refs' in path:
    payload = json.load(sys.stdin)
    print(json.dumps({{'object': {{'sha': payload['sha']}}}}))
elif path.endswith('/dispatches'):
    payload = json.load(sys.stdin)
    response = {{'run_url': 'https://api.example/runs/123', 'html_url': 'https://example/runs/123'}}
    if not {missing_run_id!r}: response['workflow_run_id'] = 123
    print(json.dumps(response))
elif path.endswith('/actions/runs/123'):
    print(json.dumps({{'id': 123, 'workflow_id': 77, 'event': 'workflow_dispatch',
      'head_branch': 'ci/sync-control-{self.sha[:12]}', 'head_sha': {json.dumps((head_sha or self.sha))},
      'display_title': 'OpenWrt {self.sync_id} {self.source}', 'run_attempt': {attempt}, 'html_url': 'https://example/runs/123'}}))
else:
    print(json.dumps({{'unexpected': args}}))
'''
        executable.write_text(script)
        executable.chmod(0o755)
        return binary

    def run_dispatch(self, binary, *, retry_existing=False):
        env = os.environ.copy()
        env["PATH"] = str(binary) + os.pathsep + env["PATH"]
        argv = [
            "python3", str(DISPATCH), "--repository", "example/lede",
            "--workflow-sha", self.sha, "--source-sha", self.source,
            "--base-sha", self.base, "--upstream-sha", self.upstream,
            "--merge-base-sha", self.merge_base, "--feed-lock-sha256", self.lock_sha,
            "--sync-id", self.sync_id,
        ]
        if retry_existing:
            argv.append("--retry-existing")
        return subprocess.run(argv, env=env, text=True, capture_output=True)

    def test_dispatch_uses_immutable_control_tag_and_records_exact_run_attempt(self):
        binary = self.fake_gh()
        result = self.run_dispatch(binary)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["run_id"], 123)
        self.assertEqual(record["run_attempt"], 1)
        self.assertFalse(record["reused"])
        self.assertEqual(record["control_ref"], f"refs/tags/ci/sync-control-{self.sha[:12]}")
        pending = json.loads((self.root / "pending-status.json").read_text())
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["context"], "upstream/ready")

    def test_duplicate_schedule_reuses_active_or_successfully_validated_exact_run(self):
        active = {
            "id": 124, "workflow_id": 77, "event": "workflow_dispatch",
            "head_branch": f"ci/sync-control-{self.sha[:12]}", "head_sha": self.sha,
            "display_title": f"OpenWrt {self.sync_id} {self.source}",
            "status": "in_progress", "run_attempt": 1,
            "html_url": "https://example/runs/124",
        }
        active_result = self.run_dispatch(self.fake_gh(existing_runs=[active]))
        self.assertEqual(active_result.returncode, 0, active_result.stderr)
        active_record = json.loads(active_result.stdout)
        self.assertTrue(active_record["reused"])
        self.assertEqual(active_record["run_id"], 124)
        forced_while_active = self.run_dispatch(self.fake_gh(existing_runs=[active]), retry_existing=True)
        self.assertEqual(forced_while_active.returncode, 0, forced_while_active.stderr)
        self.assertTrue(json.loads(forced_while_active.stdout)["reused"])

        complete = dict(active, id=123, status="completed", conclusion="success",
                        html_url="https://example/runs/123")
        ready_result = self.run_dispatch(self.fake_gh(existing_runs=[complete], ready=True))
        self.assertEqual(ready_result.returncode, 0, ready_result.stderr)
        ready_record = json.loads(ready_result.stdout)
        self.assertTrue(ready_record["reused"])
        self.assertIn("unexpired artifact", ready_record["reuse_reason"])

        expired_result = self.run_dispatch(self.fake_gh(existing_runs=[complete], ready=True, expired=True))
        self.assertNotEqual(expired_result.returncode, 0)
        self.assertIn("explicit retry", expired_result.stderr)
        retry = self.run_dispatch(
            self.fake_gh(existing_runs=[complete], ready=True, expired=True), retry_existing=True,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse(json.loads(retry.stdout)["reused"])

    def test_dispatch_rejects_wrong_attempt_head_sha_and_missing_run_id(self):
        for options, message in (({"attempt": 2}, "run attempt"), ({"head_sha": "9" * 40}, "workflow SHA"), ({"missing_run_id": True}, "workflow_run_id")):
            with self.subTest(options=options):
                result = self.run_dispatch(self.fake_gh(**options))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def candidate_fixture(self):
        manifest = {
            "base_sha": self.base,
            "upstream_sha": self.upstream,
            "merge_base_sha": self.merge_base,
            "workflow_sha": self.sha,
            "merge_commit_sha": "6" * 40,
            "feed_lock_sha256": self.lock_sha,
            "sync_id": self.sync_id,
        }
        impact = {"schema_version": 1, "platforms": {}}
        metadata = {
            "run_id": "123", "run_attempt": "2", "workflow_sha": self.sha,
            "source_sha": self.source, "sync_id": self.sync_id,
            "feed_lock_sha256": self.lock_sha, "required_jobs": ["validate_source", "build_openwrt"],
            "workflow_file": ".github/workflows/openwrt-ci.yml",
            "profile": "x86_64-passwall-docker", "repository": "example/lede",
            "dispatch_sha": self.sha,
            "platform_impact": impact,
            "feed_revisions": [],
            "validation": {"source_sha_match": True, "feed_lock_match": True,
                           "candidate_identity_match": True, "feed_revisions_collected": True},
            "event": "workflow_dispatch",
            "candidate": dict(manifest),
        }
        event = {"workflow_run": {"id": 123, "run_attempt": 2}}
        run = {"id": 123, "run_attempt": 2, "workflow_id": 77, "status": "completed", "conclusion": "success",
               "event": "workflow_dispatch", "head_branch": f"ci/sync-control-{self.sha[:12]}",
               "head_sha": self.sha, "display_title": f"OpenWrt {self.sync_id} {self.source}"}
        jobs = [{"name": name, "conclusion": "success"} for name in ("validate_source", "build_openwrt")]
        pr = {"number": 8, "head": {"sha": self.source, "ref": f"sync/{self.sync_id}"},
              "base": {"sha": self.base, "ref": "master"}, "user": {"login": "pr-author"}}
        return event, run, jobs, metadata, manifest, pr

    def evaluate(self, *, jobs=None, metadata=None, pr=None, current_default=None, artifact=True,
                 run=None, event=None, impact=None, workflow_matches=True,
                 merge_parents=True, candidate_chain=True, comments=None, reviews=None):
        event0, run0, jobs0, metadata0, manifest, pr0 = self.candidate_fixture()
        impact = impact or {"schema_version": 1, "platforms": {}}
        files = {}
        for name, value in (("event", event or event0), ("run", run or run0),
                            ("jobs", jobs if jobs is not None else jobs0),
                            ("metadata", metadata if metadata is not None else metadata0),
                            ("lock", self.lock), ("manifest", manifest), ("pr", pr if pr is not None else pr0),
                            ("impact", impact), ("evidence", {"comments": comments or [], "review_states": reviews or {}})):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(value))
            files[name] = path
        argv = ["python3", str(RESULT), "--evaluate", "--event-json", str(files["event"]),
                "--run-json", str(files["run"]), "--jobs-json", str(files["jobs"]),
                "--metadata-json", str(files["metadata"]), "--lock-json", str(files["lock"]),
                "--manifest-json", str(files["manifest"]), "--pull-request-json", str(files["pr"]),
                "--impact-plan-json", str(files["impact"]), "--evidence-json", str(files["evidence"]),
                "--workflow-id", "77", "--default-branch", "master",
                "--repository", "example/lede",
                "--current-default-sha", current_default or self.base]
        if artifact:
            argv.append("--artifact-present")
        if workflow_matches:
            argv.append("--workflow-source-matches-base")
        if merge_parents:
            argv.append("--merge-parents-match")
        if candidate_chain:
            argv.append("--candidate-merge-chain-matches")
        result = subprocess.run(argv, text=True, capture_output=True)
        return result, json.loads(result.stdout)

    def test_candidate_result_requires_all_successful_jobs_exact_attempt_and_unexpired_artifact(self):
        result, evaluation = self.evaluate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(evaluation["state"], "success")
        for options, fragment in (
            ({"jobs": [{"name": "validate_source", "conclusion": "success"}, {"name": "build_openwrt", "conclusion": "skipped"}]}, "skipped"),
            ({"artifact": False}, "expired"),
            ({"current_default": "6" * 40}, "advanced"),
        ):
            with self.subTest(fragment=fragment):
                failed, data = self.evaluate(**options)
                self.assertNotEqual(failed.returncode, 0)
                self.assertTrue(any(fragment in message for message in data["errors"]))

    def test_validation_artifact_reader_accepts_only_small_unique_metadata(self):
        files = {
            "sync-validation.json": json.dumps({"source_sha": self.source}),
            "feed-lock.json": json.dumps(self.lock),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        metadata, lock = RESULT_MODULE.read_validation_artifact(buffer.getvalue())
        self.assertEqual(metadata["source_sha"], self.source)
        self.assertEqual(lock, self.lock)

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../sync-validation.json", files["sync-validation.json"])
            archive.writestr("feed-lock.json", files["feed-lock.json"])
        with self.assertRaises(RESULT_MODULE.ResultError):
            RESULT_MODULE.read_validation_artifact(unsafe.getvalue())

    def test_wrong_run_attempt_and_candidate_head_change_cannot_pass(self):
        event, run, jobs, metadata, manifest, pr = self.candidate_fixture()
        metadata["run_attempt"] = "1"
        failed, data = self.evaluate(metadata=metadata)
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(any("attempt" in message for message in data["errors"]))

        _, _, _, metadata, _, _ = self.candidate_fixture()
        pr["head"]["sha"] = "9" * 40
        failed, data = self.evaluate(pr=pr)
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(any("HEAD changed" in message for message in data["errors"]))

        stale_event = {"workflow_run": {"id": 123, "run_attempt": 1}}
        latest_run = dict(run, run_attempt=2)
        ignored, stale = self.evaluate(event=stale_event, run=latest_run)
        self.assertEqual(ignored.returncode, 0, ignored.stdout + ignored.stderr)
        self.assertEqual(stale["mode"], "stale_attempt_ignored")
        self.assertIsNone(stale["target_sha"])

    def test_later_attempts_for_same_candidate_or_pull_request_suppress_old_results(self):
        _, candidate, *_ = self.candidate_fixture()
        newer_candidate = dict(candidate, id=124, run_attempt=1)
        self.assertTrue(RESULT_MODULE.is_superseded_run(candidate, [newer_candidate]))
        different_candidate = dict(newer_candidate, display_title=f"OpenWrt upstream-aaaaaaaaaaaa-bbbbbbbbbbbb-wcccccccccccc {self.source}")
        self.assertFalse(RESULT_MODULE.is_superseded_run(candidate, [different_candidate]))

        pull_request = {
            "id": 200, "workflow_id": 77, "event": "pull_request", "head_branch": "feature/fix",
            "head_repository": {"full_name": "example/fork"}, "pull_requests": [{"number": 8}],
        }
        newer_pr = dict(pull_request, id=201, pull_requests=[{"number": 8}])
        unrelated_pr = dict(pull_request, id=202, head_branch="feature/other", pull_requests=[{"number": 9}])
        self.assertTrue(RESULT_MODULE.is_superseded_run(pull_request, [newer_pr]))
        self.assertFalse(RESULT_MODULE.is_superseded_run(pull_request, [unrelated_pr]))

    def test_normal_pull_request_is_aggregated_and_candidate_preview_is_ignored(self):
        event, run, jobs, metadata, manifest, pr = self.candidate_fixture()
        run.update({
            "event": "workflow_dispatch", "head_sha": self.base,
            "head_branch": f"ci/sync-control-{self.base[:12]}",
            "display_title": f"OpenWrt pr-8 {self.source}",
            "path": f".github/workflows/openwrt-ci.yml@refs/tags/ci/sync-control-{self.base[:12]}",
        })
        metadata.update({"event": "workflow_dispatch", "sync_id": None, "candidate": {},
                         "workflow_sha": self.base, "dispatch_sha": self.base,
                         "pull_request": {"number": 8, "head_sha": "7" * 40,
                                           "base_sha": self.base, "merge_sha": self.source}})
        pr.update({"head": {"sha": "7" * 40, "ref": "feature/fix"},
                   "merge_commit_sha": self.source})
        event = {"workflow_run": {"id": 123, "run_attempt": 2}}
        result, evaluation = self.evaluate(run=run, metadata=metadata, pr=pr, event=event)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(evaluation["mode"], "pull_request")

        pr["merge_commit_sha"] = "8" * 40
        stale_merge, stale_data = self.evaluate(run=run, metadata=metadata, pr=pr, event=event)
        self.assertNotEqual(stale_merge.returncode, 0)
        self.assertTrue(any("merge SHA" in message for message in stale_data["errors"]))

        pr["merge_commit_sha"] = self.source
        changed_workflow, changed_data = self.evaluate(
            run=run, metadata=metadata, pr=pr, event=event, workflow_matches=False,
        )
        self.assertNotEqual(changed_workflow.returncode, 0)
        self.assertTrue(any("workflow" in message for message in changed_data["errors"]))
        deleted_compile, deleted_data = self.evaluate(
            run=run, metadata=metadata, pr=pr, event=event, workflow_matches=False,
        )
        self.assertNotEqual(deleted_compile.returncode, 0)
        self.assertTrue(any("workflow" in message for message in deleted_data["errors"]))
        changed_parents, parent_data = self.evaluate(
            run=run, metadata=metadata, pr=pr, event=event, merge_parents=False,
        )
        self.assertNotEqual(changed_parents.returncode, 0)
        self.assertTrue(any("parents" in message for message in parent_data["errors"]))

        forged = dict(metadata)
        forged["workflow_sha"] = "9" * 40
        forged["dispatch_sha"] = "9" * 40
        forged_w, forged_data = self.evaluate(run=run, metadata=forged, pr=pr, event=event)
        self.assertNotEqual(forged_w.returncode, 0)
        self.assertTrue(any("trusted PR base" in message or "workflow" in message for message in forged_data["errors"]))

        forged = dict(metadata)
        forged["pull_request"] = {"number": 8, "head_sha": "a" * 40,
                                  "base_sha": "b" * 40, "merge_sha": "c" * 40}
        forged_pr, forged_pr_data = self.evaluate(run=run, metadata=forged, pr=pr, event=event)
        self.assertNotEqual(forged_pr.returncode, 0)
        self.assertTrue(any("HEAD changed" in message or "base SHA" in message or "merge SHA" in message
                            for message in forged_pr_data["errors"]))

        pr["head"]["ref"] = "sync/upstream-333333333333-444444444444"
        run.update({"event": "pull_request"})
        failed, data = self.evaluate(run=run, metadata=metadata, pr=pr, event=event)
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(data["state"], "neutral")
        self.assertEqual(data["mode"], "candidate_pull_request_preview_ignored")

    def test_workflow_source_blob_must_match_trusted_pr_base(self):
        with mock.patch.object(RESULT_MODULE, "content_blob_sha", side_effect=["base-blob", "base-blob"]):
            self.assertTrue(RESULT_MODULE.same_workflow_file("example/lede", "base", "merge"))
        # Replacing or deleting a real compile step changes the effective workflow blob.
        with mock.patch.object(
            RESULT_MODULE, "content_blob_sha",
            side_effect=["trusted-base-blob", "workflow-without-build-blob"],
        ):
            self.assertFalse(RESULT_MODULE.same_workflow_file("example/lede", "base", "merge"))
        with mock.patch.object(
            RESULT_MODULE, "content_blob_sha",
            side_effect=["trusted-base-blob", RESULT_MODULE.ResultError("workflow file was deleted")],
        ):
            self.assertFalse(RESULT_MODULE.same_workflow_file("example/lede", "base", "merge"))

    def test_candidate_platform_evidence_is_bound_to_the_exact_candidate_sha(self):
        event, run, jobs, metadata, manifest, pr = self.candidate_fixture()
        changed_paths = ["target/linux/qualcommax/image/ipq60xx.mk"]
        plan = IMPACT_MODULE.plan_for_paths(changed_paths)
        self.assertEqual(
            plan["platforms"]["qualcomm_ipq60xx"],
            ["image_build", "dts_review", "upgrade_review"],
        )
        metadata["platform_impact"] = plan
        comments = []
        for platform, checks in plan["platforms"].items():
            for check in checks:
                evidence = {
                    "candidate_sha": self.source, "platform": platform, "check": check,
                    "result": "pass", "evidence_url": "https://example.invalid/evidence/123",
                    "sha256": "d" * 64,
                }
                comments.append({
                    "user": {"login": "maintainer"},
                    "body": "<!-- upstream-sync-evidence:" + json.dumps(evidence) + " -->",
                })
        approved = {"maintainer": {"state": "APPROVED", "commit_id": self.source}}
        accepted, accepted_data = self.evaluate(
            metadata=metadata, impact=plan, comments=comments, reviews=approved,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(accepted_data["state"], "success")

        changed_candidate_sha = "8" * 40
        changed_run = dict(run, display_title=f"OpenWrt {self.sync_id} {changed_candidate_sha}")
        changed_metadata = json.loads(json.dumps(metadata))
        changed_metadata["source_sha"] = changed_candidate_sha
        changed_pr = json.loads(json.dumps(pr))
        changed_pr["head"]["sha"] = changed_candidate_sha
        stale, stale_data = self.evaluate(
            run=changed_run, metadata=changed_metadata, pr=changed_pr,
            impact=plan, comments=comments,
            reviews={"maintainer": {"state": "APPROVED", "commit_id": changed_candidate_sha}},
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(stale_data["state"], "pending")
        self.assertTrue(any("platform evidence pending" in error for error in stale_data["errors"]))

    def test_comment_reaggregation_clears_old_success_before_api_or_evidence_failure(self):
        with tempfile.TemporaryDirectory(prefix="lede-comment-aggregate-") as temporary:
            event_path = Path(temporary) / "event.json"
            event_path.write_text(json.dumps({
                "repository": {"default_branch": "master"},
                "issue": {"number": 9, "pull_request": {"url": "https://api.example/pulls/9"}},
            }))
            pr = {
                "number": 9, "state": "open", "draft": False, "html_url": "https://example/pr/9",
                "head": {"ref": "feature/fix", "sha": "7" * 40,
                         "repo": {"full_name": "example/lede"}},
                "base": {"ref": "master", "sha": self.base},
                "merge_commit_sha": self.source,
            }
            with mock.patch.dict(os.environ, {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": "example/lede",
                "GITHUB_EVENT_NAME": "issue_comment",
            }), mock.patch.object(RESULT_MODULE, "gh_json", side_effect=[{"id": 77}, pr]), \
                    mock.patch.object(RESULT_MODULE, "result_run_list", return_value=[]), \
                    mock.patch.object(RESULT_MODULE.subprocess, "run") as api_call:
                with self.assertRaisesRegex(RESULT_MODULE.ResultError, "no exact build run"):
                    RESULT_MODULE.run_gh()

            self.assertEqual(api_call.call_count, 1)
            command = api_call.call_args.args[0]
            self.assertTrue(command[4].endswith("/statuses/" + self.source))
            self.assertEqual(json.loads(api_call.call_args.kwargs["input"])["state"], "pending")

    def test_x86_success_cannot_replace_affected_platform_evidence(self):
        event, run, jobs, metadata, manifest, pr = self.candidate_fixture()
        run.update({
            "event": "workflow_dispatch", "head_sha": self.base,
            "head_branch": f"ci/sync-control-{self.base[:12]}",
            "display_title": f"OpenWrt pr-8 {self.source}",
            "path": f".github/workflows/openwrt-ci.yml@refs/tags/ci/sync-control-{self.base[:12]}",
        })
        metadata.update({
            "event": "workflow_dispatch", "sync_id": None, "candidate": {},
            "workflow_sha": self.base, "dispatch_sha": self.base,
            "pull_request": {"number": 8, "head_sha": "7" * 40,
                             "base_sha": self.base, "merge_sha": self.source},
        })
        pr.update({"head": {"sha": "7" * 40, "ref": "feature/fix"},
                   "merge_commit_sha": self.source})
        event = {"workflow_run": {"id": 123, "run_attempt": 2}}
        paths = {
            "qualcomm": ["target/linux/qualcommax/image/ipq60xx.mk"],
            "airoha": ["target/linux/airoha/image/an7581.mk"],
            "rockchip": ["target/linux/rockchip/patches-6.18/board.patch"],
        }
        for label, changed in paths.items():
            with self.subTest(platform=label):
                plan = IMPACT_MODULE.plan_for_paths(changed)
                metadata["platform_impact"] = plan
                result, data = self.evaluate(run=run, metadata=metadata, pr=pr,
                                             event=event, impact=plan)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(data["state"], "pending")
                self.assertTrue(any("platform evidence pending" in error for error in data["errors"]))

        plan = IMPACT_MODULE.plan_for_paths(paths["qualcomm"])
        metadata["platform_impact"] = plan
        comments = []
        for platform, checks in plan["platforms"].items():
            for check in checks:
                marker = {
                    "candidate_sha": self.source,
                    "platform": platform,
                    "check": check,
                    "result": "pass",
                    "evidence_url": "https://example.invalid/evidence/123",
                    "sha256": "d" * 64,
                }
                comments.append({
                    "user": {"login": "maintainer"},
                    "body": "<!-- upstream-sync-evidence:" + json.dumps(marker) + " -->",
                })
        passed, result_data = self.evaluate(
            run=run, metadata=metadata, pr=pr, event=event, impact=plan,
            comments=comments,
            reviews={"maintainer": {"state": "APPROVED", "commit_id": "7" * 40}},
        )
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(result_data["state"], "success")

        changed_sha = "8" * 40
        changed_pr = json.loads(json.dumps(pr))
        changed_pr["merge_commit_sha"] = changed_sha
        changed_metadata = json.loads(json.dumps(metadata))
        changed_metadata["source_sha"] = changed_sha
        changed_metadata["pull_request"]["merge_sha"] = changed_sha
        changed_run = dict(run, display_title=f"OpenWrt pr-8 {changed_sha}")
        old_comments = comments
        stale_evidence, stale_data = self.evaluate(
            run=changed_run, metadata=changed_metadata, pr=changed_pr,
            event=event, impact=plan, comments=old_comments,
            reviews={"maintainer": {"state": "APPROVED", "commit_id": "7" * 40}},
        )
        self.assertNotEqual(stale_evidence.returncode, 0)
        self.assertEqual(stale_data["state"], "pending")
        self.assertTrue(any("platform evidence pending" in error for error in stale_data["errors"]))

    def test_workflows_keep_manual_mode_off_schedule_and_isolate_release_writes(self):
        sync = yaml.load((ROOT / ".github/workflows/sync-upstream.yml").read_text(), Loader=yaml.BaseLoader)
        ci = yaml.load((ROOT / ".github/workflows/openwrt-ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(list(sync["on"].keys()), ["workflow_dispatch"])
        self.assertEqual(sync["on"]["workflow_dispatch"]["inputs"]["mode"]["default"], "prepare")
        self.assertNotIn("schedule", sync["on"])
        self.assertEqual(ci["permissions"]["contents"], "read")
        self.assertNotIn("if", ci["jobs"]["validate_source"])
        build = ci["jobs"]["build_openwrt"]
        self.assertNotIn("permissions", build)
        self.assertFalse(any("gh release" in step.get("run", "") or "git push" in step.get("run", "") for step in build["steps"]))
        publisher = ci["jobs"]["publish_release"]
        self.assertEqual(publisher["permissions"]["contents"], "write")
        self.assertIn("inputs.sync_id == ''", publisher["if"])
        self.assertIn("inputs.pull_request == ''", publisher["if"])
        self.assertTrue(any("default branch" in step.get("name", "").lower() for step in publisher["steps"]))
        self.assertEqual(set(ci["on"].keys()), {"workflow_dispatch"})
        build_runs = "\n".join(step.get("run", "") for step in build["steps"])
        publisher_runs = "\n".join(step.get("run", "") for step in publisher["steps"])
        self.assertIn("scripts/release-notes.py", build_runs)
        self.assertIn("scripts/release-notes.py", publisher_runs)
        self.assertNotIn('cat > "$notes_file" <<EOF', build_runs)

        stale_step = next(
            step for step in sync["jobs"]["prepare_candidate"]["steps"]
            if step.get("name") == "Report a candidate made stale by default-branch movement"
        )
        self.assertIn('"candidate_expired"', stale_step["run"])
        status_step = next(
            step for step in sync["jobs"]["prepare_candidate"]["steps"]
            if step.get("name") == "Clear ready status for an expired candidate"
        )
        self.assertIn("/statuses/${candidate_sha}", status_step["run"])
        self.assertIn('"state": "failure"', status_step["run"])
        self.assertIn("inputs.mode != 'report'", status_step["if"])
        self.assertIn("inputs.dry_run != true", status_step["if"])

        for step in sync["jobs"]["prepare_candidate"]["steps"]:
            run = step.get("run", "")
            writes_remote = any(marker in run for marker in (
                "git push ", "gh pr create", "scripts/sync-dispatch.py", "gh api --method POST",
            ))
            if writes_remote:
                self.assertIn("inputs.mode != 'report'", step.get("if", ""), step.get("name"))
                self.assertIn("inputs.dry_run != true", step.get("if", ""), step.get("name"))

        pr_dispatch = yaml.load((ROOT / ".github/workflows/pr-build-dispatch.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("pull_request_target", pr_dispatch["on"])
        self.assertEqual(pr_dispatch["permissions"]["contents"], "write")
        dispatch_script = "\n".join(step.get("run", "") for step in pr_dispatch["jobs"]["dispatch_read_only_build"]["steps"])
        self.assertIn("scripts/pr-build-dispatch.py", dispatch_script)
        self.assertNotIn("github.event.pull_request.head.sha", dispatch_script)
        self.assertNotIn("make ", dispatch_script)
        self.assertEqual(
            pr_dispatch["jobs"]["dispatch_read_only_build"]["steps"][0]["with"]["persist-credentials"],
            "false",
        )


class ReleaseNotesTests(unittest.TestCase):
    def test_notes_generator_preserves_markdown_fields_without_executing_shell_text(self):
        with tempfile.TemporaryDirectory(prefix="lede-release-notes-") as temporary:
            root = Path(temporary)
            marker = root / "command-was-executed"
            hostile = f"profile`$($(touch {marker}))\nnext line"
            output = root / "notes.md"
            result = subprocess.run([
                "python3", str(ROOT / "scripts/release-notes.py"),
                "--profile", hostile,
                "--commit", f"sha`$(touch {marker})`$value\nsecond",
                "--workflow-commit", "w" * 40,
                "--feed-lock-sha256", "a" * 64,
                "--run-url", "https://github.com/example/lede/actions/runs/12",
                "--built-at", "2026-10-14T00:00:00Z",
                "--output", str(output),
            ], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            notes = output.read_text()
            self.assertFalse(marker.exists())
            self.assertIn("Automated build for", notes)
            self.assertIn("- Commit:", notes)
            self.assertIn("- Workflow commit:", notes)
            self.assertIn("- Feed lock SHA-256:", notes)
            self.assertIn("- Run: https://github.com/example/lede/actions/runs/12", notes)
            self.assertIn(r"\n", notes)
            self.assertIn("$value", notes)
            self.assertIn("2026-10-14T00:00:00Z", notes)


class PullRequestDispatchTests(unittest.TestCase):
    def setUp(self):
        self.repository = "example/lede"
        self.workflow = "1" * 40
        self.head = "2" * 40
        self.base = self.workflow
        self.merge = "3" * 40

    def fake_api(self, *, run_attempt=1, run_head=None, live_merge=None):
        state = {"tag": None, "pending": None, "dispatch": None}

        def gh(args, *, input_data=None, check=True):
            paths = [item for item in args if item.startswith("repos/")]
            path = paths[-1]
            method = args[args.index("--method") + 1] if "--method" in args else "GET"
            payload = json.loads(input_data) if input_data else None
            if path == f"repos/{self.repository}/pulls/8":
                value = {
                    "number": 8, "state": "open", "merge_commit_sha": live_merge or self.merge,
                    "head": {"sha": self.head, "ref": "feature/fix"},
                    "base": {"sha": self.base, "ref": "master"},
                }
            elif path == f"repos/{self.repository}":
                value = {"default_branch": "master"}
            elif path == f"repos/{self.repository}/branches/master":
                value = {"commit": {"sha": self.base}}
            elif path == f"repos/{self.repository}/actions/workflows/openwrt-ci.yml":
                value = {"id": 77, "path": ".github/workflows/openwrt-ci.yml", "state": "active"}
            elif "actions/workflows/77/runs?" in path:
                value = {"workflow_runs": []}
            elif "/git/ref/tags/" in path:
                if state["tag"]:
                    value = {"object": {"sha": state["tag"]}}
                else:
                    result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"Not Found")
                    if check:
                        raise PR_DISPATCH_MODULE.DispatchError("Not Found")
                    return result
            elif path.endswith("/git/refs") and method == "POST":
                state["tag"] = payload["sha"]
                value = {"object": {"sha": payload["sha"]}}
            elif path.endswith(f"/statuses/{self.merge}") and method == "POST":
                state["pending"] = payload
                value = payload
            elif path.endswith("/dispatches") and method == "POST":
                state["dispatch"] = payload
                value = {"workflow_run_id": 123}
            elif path.endswith("/actions/runs/123"):
                value = {
                    "id": 123, "workflow_id": 77, "event": "workflow_dispatch",
                    "head_branch": f"ci/sync-control-{self.workflow[:12]}",
                    "head_sha": run_head or self.workflow,
                    "display_title": f"OpenWrt pr-8 {self.merge}",
                    "run_attempt": run_attempt, "html_url": "https://example.invalid/run/123",
                }
            else:
                raise AssertionError(f"unexpected mocked GitHub API path: {path} {args!r}")
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(value).encode(), stderr=b"")

        return gh, state

    def test_dispatch_binds_run_id_attempt_and_exact_pr_shas(self):
        gh, state = self.fake_api()
        with mock.patch.object(PR_DISPATCH_MODULE, "gh", side_effect=gh):
            record = PR_DISPATCH_MODULE.dispatch(
                self.repository, self.workflow, 8, self.head, self.base, self.merge,
            )
        self.assertEqual(record["run_id"], 123)
        self.assertEqual(record["run_attempt"], 1)
        self.assertFalse(record["reused"])
        self.assertEqual(state["tag"], self.workflow)
        self.assertEqual(state["pending"]["state"], "pending")
        self.assertEqual(state["dispatch"]["inputs"]["pull_merge_sha"], self.merge)
        self.assertEqual(state["dispatch"]["inputs"]["pull_head_sha"], self.head)

    def test_changed_pr_or_misassociated_run_is_rejected(self):
        for options, fragment in (
            ({"live_merge": "9" * 40}, "merge SHA changed"),
            ({"run_attempt": 2}, "attempt 1"),
            ({"run_head": "9" * 40}, "wrong trusted control ref"),
        ):
            with self.subTest(options=options):
                gh, _ = self.fake_api(**options)
                with mock.patch.object(PR_DISPATCH_MODULE, "gh", side_effect=gh):
                    with self.assertRaisesRegex(PR_DISPATCH_MODULE.DispatchError, fragment):
                        PR_DISPATCH_MODULE.dispatch(
                            self.repository, self.workflow, 8, self.head, self.base, self.merge,
                        )


if __name__ == "__main__":
    unittest.main()
