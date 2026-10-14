#!/usr/bin/env python3
"""Test CI provenance and publication using temporary Git repositories.

Run with: python3 -m unittest discover -s scripts/tests -v
The workflow integration tests require PyYAML.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT = ROOT / "scripts/ci-source-info.sh"
WORKFLOW = ROOT / ".github/workflows/openwrt-ci.yml"


class CISourceInfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lede-ci-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "CI Test",
            "GIT_COMMITTER_NAME": "CI Test",
            "GIT_AUTHOR_EMAIL": "ci@example.invalid",
            "GIT_COMMITTER_EMAIL": "ci@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        })
        self.git("init", "--quiet", "--initial-branch=master")
        self.tree = self.git("mktree", input="")
        self.base = self.commit("base")
        self.default = self.commit("default branch", self.base)
        self.upstream = self.commit("upstream", self.base)
        self.candidate = self.commit("candidate merge", self.default, self.upstream)
        self.git("update-ref", "refs/remotes/origin/master", self.default)
        self.checkout(self.default)

    def git(self, *args, input=None):
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, input=input,
            text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    def commit(self, message, *parents, tree=None):
        args = ["-c", "commit.gpgsign=false", "commit-tree", tree or self.tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, input=message + "\n")

    def checkout(self, sha):
        self.git("checkout", "--quiet", "--detach", sha)

    def run_source(self, expected=None, ref="refs/heads/master", default_branch="master"):
        env = self.env | {
            "DEFAULT_BRANCH": default_branch,
            "GITHUB_REF": ref,
            "GITHUB_SHA": self.default,
            "EXPECTED_SOURCE_SHA": expected or self.git("rev-parse", "HEAD"),
        }
        return subprocess.run(
            ["bash", str(SOURCE_SCRIPT)], cwd=self.repo, env=env,
            text=True, capture_output=True,
        )

    def outputs(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_default_branch_head_can_publish(self):
        outputs = self.outputs(self.run_source())
        self.assertEqual(outputs["source_sha"], self.default)
        self.assertEqual(outputs["short_sha"], self.default[:12])
        self.assertEqual(outputs["publish_release"], "true")

    def test_merge_candidate_uses_checked_out_sha_without_publishing(self):
        self.checkout(self.candidate)
        outputs = self.outputs(self.run_source(expected=self.candidate))
        self.assertEqual(outputs["source_sha"], self.candidate)
        self.assertEqual(outputs["publish_release"], "false")

    def test_branch_dispatch_cannot_publish_even_at_default_head(self):
        outputs = self.outputs(self.run_source(ref="refs/heads/sync/upstream-reconcile"))
        self.assertEqual(outputs["publish_release"], "false")

    def test_tag_dispatch_cannot_publish(self):
        outputs = self.outputs(self.run_source(ref="refs/tags/test-build"))
        self.assertEqual(outputs["publish_release"], "false")

    def test_historical_default_branch_commit_cannot_publish(self):
        self.checkout(self.base)
        outputs = self.outputs(self.run_source(expected=self.base))
        self.assertEqual(outputs["publish_release"], "false")

    def test_default_branch_names_with_slashes(self):
        self.git("update-ref", "refs/remotes/origin/release/main", self.default)
        outputs = self.outputs(self.run_source(
            ref="refs/heads/release/main", default_branch="release/main",
        ))
        self.assertEqual(outputs["publish_release"], "true")

    def test_wrong_checkout_fails(self):
        result = self.run_source(expected=self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertNotIn("publish_release=true", result.stdout)

    def test_ref_or_abbreviated_sha_is_rejected(self):
        for expected in ["refs/heads/master", self.default[:12], "$(exit 0)"]:
            with self.subTest(expected=expected):
                result = self.run_source(expected=expected)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_missing_default_branch_reference_fails(self):
        self.git("update-ref", "-d", "refs/remotes/origin/master")
        result = self.run_source()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("publish_release=true", result.stdout)

    def workflow(self):
        # BaseLoader keeps GitHub's YAML "on" key as a string.
        return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def step(self, step_id):
        return next(step for job in self.workflow()["jobs"].values()
                    for step in job.get("steps", []) if step.get("id") == step_id)

    def test_workflow_can_build_a_source_commit_without_the_new_ci_helper(self):
        helper_paths = [
            ("ci-source-info.sh", SOURCE_SCRIPT),
            ("feed-lock.py", ROOT / "scripts/feed-lock.py"),
            ("verify-fork-contract.py", ROOT / "scripts/verify-fork-contract.py"),
        ]
        entries = []
        for name, path in helper_paths:
            blob = self.git("hash-object", "-w", "--stdin", input=path.read_text())
            mode = "100755" if name == "ci-source-info.sh" else "100644"
            entries.append(f"{mode} blob {blob}\t{name}")
        scripts_tree = self.git("mktree", input="\n".join(sorted(entries)) + "\n")
        workflow_tree = self.git("mktree", input=f"040000 tree {scripts_tree}\tscripts\n")
        workflow_sha = self.commit("workflow implementation", self.candidate, tree=workflow_tree)
        self.checkout(self.candidate)
        self.git("update-ref", "refs/heads/master", self.default)
        self.git("remote", "add", "origin", str(self.repo))
        self.assertFalse((self.repo / "scripts/ci-source-info.sh").exists())
        runner_temp = self.root / "runner"
        runner_temp.mkdir()
        output_file = self.root / "source-output"
        env = self.env | {
            "DEFAULT_BRANCH": "master", "GITHUB_REF": "refs/heads/review-workflow",
            "EXPECTED_SOURCE_SHA": self.candidate, "WORKFLOW_SHA": workflow_sha,
            "GITHUB_OUTPUT": str(output_file), "RUNNER_TEMP": str(runner_temp),
        }
        fetch = next(step for step in self.workflow()["jobs"]["validate_source"]["steps"]
                     if step.get("name", "").startswith("Fetch trusted workflow source"))
        fetched = subprocess.run(
            ["bash", "-e", "-c", fetch["run"]],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        result = subprocess.run(
            ["bash", "-e", "-c", self.step("source")["run"]],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source_sha=" + self.candidate + "\n", output_file.read_text())
        self.assertEqual((runner_temp / SOURCE_SCRIPT.name).read_bytes(), SOURCE_SCRIPT.read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.candidate)

    def test_publisher_refreshes_default_branch_before_release(self):
        publisher = self.workflow()["jobs"]["publish_release"]
        gate = next(step for step in publisher["steps"]
                    if step.get("name", "").startswith("Confirm source"))
        self.assertIn("gh api", gate["run"])
        self.assertIn("branches/${DEFAULT_BRANCH}", gate["run"])
        self.assertIn('"$SOURCE_SHA" == "$current_default"', gate["run"])
        self.assertIn('"$SOURCE_SHA" == "$GITHUB_SHA"', gate["run"])

    def test_candidate_artifact_metadata_uses_source_not_dispatch_sha(self):
        lock = {"schema_version": 1, "feeds": [], "overlays": {}}
        lock_bytes = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
        lock_sha = hashlib.sha256(lock_bytes).hexdigest()
        sync_id = "upstream-" + self.default[:12] + "-" + self.upstream[:12] + "-w" + self.default[:12]
        manifest = {
            "base_sha": self.default, "upstream_sha": self.upstream,
            "merge_base_sha": self.base, "workflow_sha": self.default,
            "feed_lock_sha256": lock_sha, "sync_id": sync_id,
        }
        objects = {}
        for path, content in (
            (".github/feed-lock.json", json.dumps(lock) + "\n"),
            (".github/upstream-candidate.json", json.dumps(manifest) + "\n"),
        ):
            objects[path] = self.git("hash-object", "-w", "--stdin", input=content)
        github_tree = self.git("mktree", input="\n".join(
            f"100644 blob {objects[path]}\t{path.split('/')[-1]}" for path in sorted(objects)
        ) + "\n")
        root_tree = self.git("mktree", input=f"040000 tree {github_tree}\t.github\n")
        source = self.commit("candidate source", self.default, self.upstream, tree=root_tree)
        self.checkout(source)
        output = self.root / "sync-validation.json"
        env = self.env | {
            "GITHUB_REPOSITORY": "example/firmware", "GITHUB_SHA": self.default,
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        }
        result = subprocess.run([
            "python3", str(ROOT / "scripts/ci-build-metadata.py"),
            "--root", str(self.repo), "--workflow-sha", self.default,
            "--source-sha", source, "--feed-lock-sha256", lock_sha,
            "--sync-id", sync_id, "--base-sha", self.default,
            "--upstream-sha", self.upstream, "--merge-base-sha", self.base,
            "--build-reason", "upstream-sync", "--output", str(output),
        ], cwd=self.repo, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(output.read_text())
        self.assertEqual(metadata["source_sha"], source)
        self.assertEqual(metadata["dispatch_sha"], self.default)
        self.assertEqual(metadata["run_attempt"], "2")
        self.assertEqual(metadata["candidate"]["sync_id"], sync_id)

    def test_workflow_allows_candidates_and_guards_all_release_writes(self):
        workflow = self.workflow()
        build = workflow["jobs"]["build_openwrt"]
        self.assertFalse(any("gh release" in step.get("run", "") or "git push" in step.get("run", "")
                             for step in build["steps"]))
        self.assertEqual(workflow["permissions"]["contents"], "read")
        publisher = workflow["jobs"]["publish_release"]
        self.assertEqual(publisher["permissions"]["contents"], "write")
        self.assertIn("github.event_name == 'workflow_dispatch'", publisher["if"])
        self.assertIn("inputs.sync_id == ''", publisher["if"])
        writers = [step for step in publisher["steps"]
                   if "gh release" in step.get("run", "") or "git push" in step.get("run", "")]
        self.assertEqual(len(writers), 3)
        upload = next(step for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertNotIn("if", upload)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], "14")


if __name__ == "__main__":
    unittest.main()
