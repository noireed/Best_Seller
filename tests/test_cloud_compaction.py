from types import SimpleNamespace
import pytest
from app.cloud_compaction import compact, checkpoint_tree, FILES, BRANCH


def test_compaction_preserves_exact_blobs_and_uses_atomic_lease(tmp_path):
    pushes=[];fetches=[];trees=[]
    def run(args,**kw):
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "secret-token" not in " ".join(args)
        result=SimpleNamespace(returncode=0,stdout="",stderr="")
        if args[1]=="fetch":fetches.append(True)
        elif args[1]=="rev-parse":result.stdout=f"{len(fetches):040x}\n"
        elif args[1]=="ls-tree":result.stdout="100644 blob "+"a"*40+"\tresearch-state.enc\n100644 blob "+"b"*40+"\tsimulation-state.enc\n"
        elif args[1]=="mktree":assert kw["text"] is False and isinstance(kw["input"],bytes) and b"\r" not in kw["input"];trees.append(kw["input"].decode());result.stdout="c"*40+"\n"
        elif "commit-tree" in args:
            assert "-p" not in args
            result.stdout="d"*40+"\n"
        elif args[1]=="push":
            pushes.append(args)
            if len(pushes)==1:result.returncode=1;result.stderr="[rejected] (stale info)"
        elif args[1]=="cat-file":result.stdout="100\n"
        return result
    result=compact(token="secret-token",workspace=tmp_path,run=run)
    assert len(pushes)==2 and len(fetches)==2
    assert f"--force-with-lease=refs/heads/{BRANCH}:"+f"{2:040x}" in pushes[-1]
    assert pushes[-1][-1]=="d"*40+":refs/heads/"+BRANCH
    assert set(result["checkpoint_blobs"])==set(FILES) and result["retained_bytes"]==200
    assert all(tree.count("\n")==2 and "README" not in tree for tree in trees)
    assert not (tmp_path/".state-maintenance").exists()


@pytest.mark.parametrize("raw", ["", "100644 blob "+"a"*40+"\tsimulation-state.enc\n",
    "100644 blob "+"a"*40+"\t.env\n", "100755 blob "+"a"*40+"\tresearch-state.enc\n"])
def test_unexpected_or_missing_state_is_never_published(raw):
    with pytest.raises(ValueError):checkpoint_tree(raw)


def test_compaction_refuses_missing_authentication(tmp_path):
    with pytest.raises(ValueError):compact(token="",workspace=tmp_path)


def test_bad_new_paths_are_rejected_before_push(tmp_path):
    pushes=[]
    def run(args,**kw):
        r=SimpleNamespace(returncode=0,stdout="",stderr="")
        if args[1]=="rev-parse":r.stdout="a"*40
        elif args[1]=="ls-tree" and args[2]=="a"*40:r.stdout="100644 blob "+"1"*40+"\tresearch-state.enc\n100644 blob "+"2"*40+"\tsimulation-state.enc\n"
        elif args[1]=="mktree":r.stdout="b"*40
        elif "commit-tree" in args:r.stdout="c"*40
        elif args[1]=="push":pushes.append(args)
        return r
    with pytest.raises(ValueError):compact(token="test",workspace=tmp_path,run=run)
    assert pushes==[]
