#!/usr/bin/env python3
"""Test CI provenance and publication using temporary Git repositories.

Run with: python3 -m unittest discover -s scripts/tests -v
The workflow integration tests require PyYAML.
"""

import os
from pathlib import Path
import shutil
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
        return next(
            step for step in self.workflow()["jobs"]["build_openwrt"]["steps"]
            if step.get("id") == step_id
        )

    def test_workflow_can_build_a_source_commit_without_the_new_ci_helper(self):
        blob = self.git("hash-object", "-w", "--stdin", input=SOURCE_SCRIPT.read_text())
        scripts_tree = self.git("mktree", input=f"100755 blob {blob}\tci-source-info.sh\n")
        workflow_tree = self.git("mktree", input=f"040000 tree {scripts_tree}\tscripts\n")
        workflow_sha = self.commit("workflow implementation", self.candidate, tree=workflow_tree)
        self.checkout(self.candidate)
        self.assertFalse((self.repo / "scripts/ci-source-info.sh").exists())
        runner_temp = self.root / "runner"
        runner_temp.mkdir()
        output_file = self.root / "source-output"
        env = self.env | {
            "DEFAULT_BRANCH": "master", "GITHUB_REF": "refs/heads/review-workflow",
            "EXPECTED_SOURCE_SHA": self.candidate, "WORKFLOW_SHA": workflow_sha,
            "GITHUB_OUTPUT": str(output_file), "RUNNER_TEMP": str(runner_temp),
        }
        result = subprocess.run(
            ["bash", "-e", "-c", self.step("source")["run"]],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source_sha=" + self.candidate + "\n", output_file.read_text())
        self.assertEqual((runner_temp / SOURCE_SCRIPT.name).read_bytes(), SOURCE_SCRIPT.read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.candidate)

    def test_late_release_check_refreshes_default_branch(self):
        remote = self.root / "origin.git"
        self.git("init", "--quiet", "--bare", str(remote))
        (remote / "objects/info/alternates").write_text(
            str(self.repo / ".git/objects") + "\n"
        )
        newer = self.commit("default branch advanced", self.default)
        self.git("--git-dir=" + str(remote), "update-ref", "refs/heads/master", newer)
        self.git("remote", "add", "origin", str(remote))
        runner_temp = self.root / "runner"
        runner_temp.mkdir()
        shutil.copy2(SOURCE_SCRIPT, runner_temp / SOURCE_SCRIPT.name)

        # The checkout-time result is stale; the real workflow gate must refresh it.
        self.assertEqual(self.outputs(self.run_source())["publish_release"], "true")
        output_file = self.root / "gate-output"
        env = self.env | {
            "DEFAULT_BRANCH": "master", "GITHUB_REF": "refs/heads/master",
            "EXPECTED_SOURCE_SHA": self.default, "GITHUB_OUTPUT": str(output_file),
            "RUNNER_TEMP": str(runner_temp),
        }
        result = subprocess.run(
            ["bash", "-e", "-c", self.step("release_gate")["run"]],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("publish_release=false\n", output_file.read_text())
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/master"), newer)

    def test_candidate_artifact_metadata_uses_source_not_dispatch_sha(self):
        self.checkout(self.candidate)
        source = self.outputs(self.run_source(expected=self.candidate))
        target = self.repo / "bin/targets/x86/64"
        packages = self.repo / "bin/packages/test"
        target.mkdir(parents=True)
        packages.mkdir(parents=True)
        (target / "firmware.img").write_bytes(b"test firmware")
        (target / "config.buildinfo").write_text("test configuration\n")
        (packages / "package.ipk").write_bytes(b"test package")
        reason = "manual $(touch should-not-exist)"
        env = self.env | {
            "GITHUB_WORKSPACE": str(self.repo), "GITHUB_REPOSITORY": "example/firmware",
            "GITHUB_WORKFLOW": "OpenWrt-CI", "GITHUB_REF": "refs/heads/master",
            "GITHUB_SHA": self.default, "GITHUB_WORKFLOW_SHA": self.default,
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_OUTPUT": str(self.root / "asset-output"),
            "PROFILE_NAME": "test-profile", "HISTORY_RELEASE_PREFIX": "build",
            "BUILD_REASON": reason, "SOURCE_SHA": source["source_sha"],
            "SOURCE_SHORT_SHA": source["short_sha"],
        }
        result = subprocess.run(
            ["bash", "-e", "-c", self.step("prepare_release")["run"]],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = next((self.repo / "release-assets").glob("*-metadata-*.txt")).read_text()
        self.assertIn("\nCommit: " + self.candidate + "\n", metadata)
        self.assertIn("Workflow Commit: " + self.default, metadata)
        self.assertIn("Build Reason: " + reason, metadata)
        notes = (self.repo / "release-notes.md").read_text()
        self.assertIn("- Commit: `" + self.candidate + "`", notes)
        self.assertFalse((self.repo / "should-not-exist").exists())
        checksum = next((self.repo / "release-assets").glob("*-sha256sums-*.txt"))
        checked = subprocess.run(
            ["sha256sum", "-c", checksum.name], cwd=checksum.parent,
            text=True, capture_output=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_workflow_allows_candidates_and_guards_all_release_writes(self):
        job = self.workflow()["jobs"]["build_openwrt"]
        self.assertNotIn("if", job)
        writers = [
            step for step in job["steps"]
            if "gh release" in step.get("run", "") or "git push" in step.get("run", "")
        ]
        self.assertEqual(len(writers), 3)
        for step in writers:
            self.assertEqual(step.get("if"), "steps.release_gate.outputs.publish_release == 'true'")
            self.assertNotIn("${GITHUB_SHA}", step["run"])
        upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "steps.release_gate.outputs.publish_release != 'true'")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")


if __name__ == "__main__":
    unittest.main()
