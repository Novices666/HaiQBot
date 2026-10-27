"""`data_dir` 是唯一根：`db_path` / `media.dir` 不写就跟它走；媒体端口默认关。"""

from __future__ import annotations

from pathlib import Path

from haiqbot.config import HaiqConfig, MediaCfg, load_config
from haiqbot.services.media_store import MediaStore


def test_media_http_defaults_to_off() -> None:
    """没写 [media] 段时不许自己监听端口（serve_host 是 0.0.0.0，开了就是全网可达）。"""
    assert MediaCfg().serve_port == 0


def test_db_path_follows_data_dir(tmp_path: Path) -> None:
    cfg = HaiqConfig(data_dir=tmp_path / "d")
    assert cfg.db_path == tmp_path / "d" / "haiq.db"


def test_media_dir_follows_data_dir(tmp_path: Path) -> None:
    cfg = HaiqConfig(data_dir=tmp_path / "d")
    assert cfg.media.dir == tmp_path / "d" / "media"


def test_explicit_paths_win(tmp_path: Path) -> None:
    cfg = HaiqConfig(
        data_dir=tmp_path / "d",
        db_path=tmp_path / "elsewhere" / "x.db",
        media=MediaCfg(dir=tmp_path / "m"),
    )
    assert cfg.db_path == tmp_path / "elsewhere" / "x.db"
    assert cfg.media.dir == tmp_path / "m"


def test_load_config_derives_from_toml(tmp_path: Path) -> None:
    """改 data_dir 一处就够：配置文件里不写 db_path / [media] dir 时自动跟着走。"""
    p = tmp_path / "haiq.toml"
    p.write_text(f'data_dir = "{tmp_path.as_posix()}/moved"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.db_path == tmp_path / "moved" / "haiq.db"
    assert cfg.media.dir == tmp_path / "moved" / "media"


def test_bare_media_cfg_falls_back(tmp_path: Path, monkeypatch) -> None:
    """裸 `MediaCfg()`（没经过 HaiqConfig 派生）不能把 None 直接塞进 Path()。"""
    monkeypatch.chdir(tmp_path)
    store = MediaStore(object(), MediaCfg())
    assert store.dir == Path("data/media")
