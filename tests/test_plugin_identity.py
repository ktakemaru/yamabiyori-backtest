"""plugin_confidence_snapshot.plugin_identity の `git --no-optional-locks` 化で、スナップショットの envelope (git hash・branch・
dirty・変更ファイル・sha256・version) が変わらないことを確認する。修正前の呼び方 (フラグ無し) は一時リポジトリにだけ使い、本体には使わない。"""
import json
import shutil
import subprocess

import pytest

from backtest import plugin_confidence_snapshot as pcs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git が無い")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                    *args], check=True, capture_output=True)


def make_repo(root):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for f in pcs.PLUGIN_FILES:
        (root / f).write_text(f"# {f}\n", encoding="utf-8")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def identity_without_flag(monkeypatch, plugin_dir):
    """修正前と同じ: git の引数から --no-optional-locks を抜いて呼ぶ。"""
    real = subprocess.run

    def run(cmd, *a, **kw):
        return real([x for x in cmd if x != "--no-optional-locks"], *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(pcs.subprocess, "run", run)
        return pcs.plugin_identity(plugin_dir)


def states(tmp_path):
    clean = make_repo(tmp_path / "clean")
    dirty = make_repo(tmp_path / "dirty")
    (dirty / "mountain_weather_core.py").write_text("# changed\n", encoding="utf-8")
    untracked = make_repo(tmp_path / "untracked")
    (untracked / "scratch.py").write_text("x = 1\n", encoding="utf-8")      # --untracked-files=no なので dirty にならない
    detached = make_repo(tmp_path / "detached")
    git(detached, "checkout", "-q", "--detach")
    nogit = tmp_path / "nogit"
    nogit.mkdir(parents=True)
    (nogit / "mountain_weather_core.py").write_text("# plain\n", encoding="utf-8")
    return {"clean": clean, "dirty": dirty, "untracked": untracked, "detached": detached, "nogit": nogit}


def test_envelope_identical_before_and_after_flag(tmp_path, monkeypatch):
    for name, repo in states(tmp_path).items():
        new = pcs.plugin_identity(repo)
        old = identity_without_flag(monkeypatch, repo)
        assert new == old, name
    s = states(tmp_path / "again")
    assert pcs.plugin_identity(s["clean"])["git_dirty_tracked"] is False
    # 既存の挙動: stdout.strip() で porcelain 1 行目の先頭の空白が落ちる (修正前後で同じ。envelope を変えないため直さない)
    assert pcs.plugin_identity(s["dirty"])["git_dirty_files"] == ["M mountain_weather_core.py"]
    assert pcs.plugin_identity(s["untracked"])["git_dirty_tracked"] is False
    assert pcs.plugin_identity(s["detached"])["git_branch"] == "HEAD"
    assert pcs.plugin_identity(s["nogit"])["git_hash"] is None and pcs.plugin_identity(s["nogit"])["git_dirty_tracked"] is None
    assert pcs.plugin_identity(s["clean"])["plugin_version"] == "9.9.9"


def test_identity_does_not_write_to_git_dir(tmp_path):
    """stat 情報が古い (内容は同じでファイルだけ触った) 状態でも、.git/index を書き換えず、ロックも残さない。"""
    repo = make_repo(tmp_path / "r")
    f = repo / "mountain_weather_core.py"
    f.write_bytes(f.read_bytes())                                        # 内容同じ・mtime だけ更新
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    ident = pcs.plugin_identity(repo)
    assert ident["git_dirty_tracked"] is False
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert not (repo / ".git" / "index.lock").exists()
