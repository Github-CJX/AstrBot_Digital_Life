from pathlib import Path
import pytest
from digital_life_host.fork_guard import validate_default_data_root, PLUGIN_ID
from digital_life_host.storage_location import resolve_data_root, selected_root, POINTER_NAME, OWNER_NAME, LEGACY_DATA_NAMES

def test_default_does_not_adopt_upstream(tmp_path):
    old=tmp_path/'astrbot_plugin_private_companion_v2'; (old/'profiles').mkdir(parents=True)
    new=tmp_path/PLUGIN_ID
    assert resolve_data_root(validate_default_data_root(new)) == new
    assert LEGACY_DATA_NAMES == ()
    assert (old/'profiles').exists()

@pytest.mark.parametrize('name',['astrbot_plugin_private_companion','astrbot_plugin_private_companion_v2','other'])
def test_reject_wrong_root(tmp_path,name):
    with pytest.raises(ValueError): validate_default_data_root(tmp_path/name)

def test_reject_symlink_to_upstream(tmp_path):
    old=tmp_path/'astrbot_plugin_private_companion'; old.mkdir()
    new=tmp_path/PLUGIN_ID; new.symlink_to(old,target_is_directory=True)
    with pytest.raises(ValueError): validate_default_data_root(new)

def test_external_storage_has_fork_name(tmp_path):
    default=tmp_path/'default'/PLUGIN_ID
    assert selected_root(default,str(tmp_path/'external')) == tmp_path/'external'/PLUGIN_ID
    assert POINTER_NAME == '.digital-life-storage-location.json'
    assert OWNER_NAME == '.digital-life-storage-owner.json'
