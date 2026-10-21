import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import deepseek_runtime as runtime
import generator
import gui_app
import local_credentials
import source_context


def test_official_price_table_and_aliases():
    usage = {"prompt_cache_hit_tokens": 1000000, "prompt_cache_miss_tokens": 1000000,
             "completion_tokens": 1000000}
    for model in ["deepseek-flash", *runtime.MODEL_ALIASES]:
        assert runtime.estimate_cost_cny(model, usage) == pytest.approx(10.04)
        assert runtime.estimate_cost_cny(model, usage, period="off_peak") == pytest.approx(5.02)
    assert runtime.estimate_cost_cny("deepseek-v4-pro", usage) == pytest.approx(36.30)
    with pytest.raises(ValueError):
        runtime.estimate_cost_cny("deepseek-flash", usage, period="guess")


def test_alias_preflight_accepts_canonical_and_rejects_unknown():
    client = Mock()
    client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="deepseek-flash")])
    balance = Mock()
    balance.__enter__ = Mock(return_value=balance)
    balance.__exit__ = Mock(return_value=False)
    balance.read.return_value = b'{"is_available":true}'
    report = runtime.preflight(client=client, api_key="fake", base_url="https://api.deepseek.com",
        required_models=["deepseek-v4-flash"], urlopen=lambda *a, **kw: balance)
    assert report["model_aliases"] == {"deepseek-v4-flash": "deepseek-flash"}
    client.chat.completions.create.assert_not_called()
    with pytest.raises(runtime.DeepSeekPreflightError):
        runtime.preflight(client=client, api_key="fake", base_url="https://api.deepseek.com",
                          required_models=["unknown"], urlopen=lambda *a, **kw: balance)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_credential_roundtrip_is_encrypted_and_corrupt_data_fails(tmp_path):
    path = tmp_path / "test.dpapi"
    fake = "isolated-test-secret-not-real"
    local_credentials.save_key(fake, path=path)
    assert fake.encode() not in path.read_bytes()
    assert local_credentials.load_key(path=path) == fake
    path.write_bytes(b"invalid encrypted data")
    with pytest.raises(RuntimeError, match="不可读"):
        local_credentials.load_key(path=path)


def test_vault_reference_no_plaintext_in_config_and_env_precedence():
    app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
    app.config = {}
    with patch.object(local_credentials, "save_key") as save:
        app._save_private_api_key("fake-key")
    save.assert_called_once_with("fake-key")
    assert "fake-key" not in json.dumps(app._global_config_payload())
    assert app.config["credential_store"] == "windows_dpapi"
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), patch.object(local_credentials, "load_key", return_value="vault"):
        assert generator.get_runtime_api_key(app.config) == "vault"
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env"}), patch.object(local_credentials, "load_key") as read:
        assert generator.get_runtime_api_key(app.config) == "env"
        read.assert_not_called()


def test_new_defaults_and_existing_model_choices():
    assert generator.default_config()["model"] == "deepseek-flash"
    preset = gui_app.MODEL_PRESETS["DeepSeek-V4"]
    assert gui_app.resolve_preset_model(preset, {}) == "deepseek-flash"
    assert gui_app.resolve_preset_model(preset, {"model": "deepseek-v4-flash"}) == "deepseek-v4-flash"
    assert gui_app.resolve_preset_model(preset, {"model": "deepseek-chat"}) == "deepseek-chat"


def test_selected_source_is_whole_and_diagnostics_do_not_contain_text():
    content = "长设定" * 2000 + "尾部关键限制"
    block, report = source_context.whole_source("角色", content, "rag_character")
    assert block.endswith(content)
    assert report["source_chars"] == report["selected_chars"] == len(content)
    assert "尾部关键限制" not in json.dumps(report, ensure_ascii=False)


def test_truth_source_retains_tail_after_old_2500_limit(tmp_path):
    content = "事实" * 2000 + "不得越权"
    (tmp_path / "时间线锚点.txt").write_text(content, encoding="utf-8")
    app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
    app.next_chap = 5
    app._append_batch_audit = Mock()
    app._filter_spoilers_from_text = lambda text, chapter: text
    with patch.dict(generator.DIRS, {"plot": str(tmp_path)}), \
         patch.object(gui_app.story_architect, "build_stage_truth_context", return_value=""), \
         patch.object(gui_app.story_architect, "build_story_context", return_value=""):
        assert content in app._load_source_of_truth_context()
    assert app._append_batch_audit.call_args.args[0]["selected_chars"] == len(content)
