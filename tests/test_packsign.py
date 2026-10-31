"""领域包可信签名：HMAC-SHA256 compute/verify 往返、篡改失效、幂等重签、信任状态四态。"""
from pathlib import Path

import pytest

from server import config, packsign


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    return b"testkey"


@pytest.fixture
def pack_dir(tmp_path):
    d = tmp_path / "signed-pack"
    d.mkdir()
    (d / "pack.yaml").write_text(
        "name: signed-pack\nversion: '0.1.0'\ndescription: 测试\n", encoding="utf-8")
    (d / "tools").mkdir()
    (d / "tools" / "greet.py").write_text("def greet():\n    pass\n", encoding="utf-8")
    (d / "skills").mkdir()
    (d / "skills" / "demo").mkdir()
    (d / "skills" / "demo" / "SKILL.md").write_text("内容\n", encoding="utf-8")
    (d / "prompt.md").write_text("提示词\n", encoding="utf-8")
    return d


def test_not_enforced_by_default():
    # conftest 未配密钥（开放模式，POC 默认）
    assert config.PACK_SIGNING_KEY == ""
    assert not packsign.is_enforced()


def test_is_enforced(key):
    assert packsign.is_enforced()


def test_compute_verify_roundtrip(key, pack_dir):
    sig = packsign.compute(pack_dir)
    assert sig.startswith("hmac-sha256:")
    assert packsign.verify(pack_dir, sig)


def test_verify_rejects_bad_format(key, pack_dir):
    assert not packsign.verify(pack_dir, "deadbeef")           # 无签名前缀
    assert not packsign.verify(pack_dir, "")                   # 空签名


def test_tamper_invalidates(key, pack_dir):
    sig = packsign.compute(pack_dir)
    (pack_dir / "prompt.md").write_text("被篡改的提示词\n", encoding="utf-8")
    assert not packsign.verify(pack_dir, sig)                  # 签名随内容变化失效


def test_manifest_signature_line_excluded_from_payload(key, pack_dir):
    """pack.yaml 的 signature 行不进签名载荷 → 重签幂等（写签名不改变内容哈希）。"""
    sig1 = packsign.sign_pack_files(pack_dir)
    assert packsign.read_signature(pack_dir) == sig1
    sig2 = packsign.sign_pack_files(pack_dir)                  # 再签一次
    assert sig1 == sig2
    assert packsign.verify(pack_dir, sig2)
    text = (pack_dir / "pack.yaml").read_text(encoding="utf-8")
    assert sum(1 for l in text.splitlines()
               if l.strip().startswith("signature:")) == 1     # 只有一行签名


def test_sign_pack_files_without_key_returns_empty(pack_dir, monkeypatch):
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "")
    assert packsign.sign_pack_files(pack_dir) == ""


def test_trust_status_four_states(key, pack_dir):
    # 1) unsigned：包无签名
    assert packsign.trust_status(pack_dir, "") == "unsigned"
    # 2) signed：签名且校验通过
    sig = packsign.sign_pack_files(pack_dir)
    assert packsign.trust_status(pack_dir, packsign.read_signature(pack_dir)) == "signed"
    # 3) invalid：内容被篡改
    (pack_dir / "prompt.md").write_text("篡改\n", encoding="utf-8")
    assert packsign.trust_status(pack_dir, sig) == "invalid"
    # 4) signed-unverified：带签名但本机未配密钥，无法校验（仅声明，不代表可信）
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "")
    try:
        assert packsign.trust_status(pack_dir, sig) == "signed-unverified"
    finally:
        monkeypatch.undo()


def test_signature_covers_subdirs(key, pack_dir):
    """签名覆盖 tools/**、skills/** 全部文件：子目录新增文件同样使旧签名失效。"""
    sig = packsign.compute(pack_dir)
    sub = pack_dir / "tools" / "extra.py"
    sub.write_text("x = 1\n", encoding="utf-8")
    assert not packsign.verify(pack_dir, sig)


def test_ac3_verify_read_failure_fail_closed(key, pack_dir, monkeypatch, capsys):
    """AC3/R5：验签路径资源读取 OSError → 不上抛、判 False（fail-closed）
    且告警可定位；trust_status 相应为 invalid；签名生成路径 compute 保持
    严格上抛（不得静默跳过不可读文件签出覆盖不完整的签名）。"""
    real_read = Path.read_bytes

    def _boom(self):
        if self.name == "greet.py":
            raise OSError("injected read failure")
        return real_read(self)

    sig = packsign.compute(pack_dir)
    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert not packsign.verify(pack_dir, sig)              # 不抛异常，判无效
    assert "读取失败" in capsys.readouterr().out            # 服务端告警可定位
    assert packsign.trust_status(pack_dir, sig) == "invalid"
    with pytest.raises(OSError):                            # 生成路径：明确报错
        packsign.compute(pack_dir)


def test_review_verify_survives_encode_error(key, pack_dir, monkeypatch, capsys):
    """评审 C6：Linux surrogateescape 文件名（lone surrogate）使 _payload
    的 rel.encode("utf-8") 抛 UnicodeEncodeError——verify 与 OSError 同
    口径 fail-closed 判 False（不上抛炸掉 scan_packs），告警可定位。"""
    sig = packsign.compute(pack_dir)

    def _boom(d):
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1,
                                 "surrogates not allowed")

    monkeypatch.setattr(packsign, "_payload", _boom)
    assert not packsign.verify(pack_dir, sig)              # 不抛异常，判无效
    assert "读取失败" in capsys.readouterr().out            # 异常类型入告警可定位
    assert packsign.trust_status(pack_dir, sig) == "invalid"
