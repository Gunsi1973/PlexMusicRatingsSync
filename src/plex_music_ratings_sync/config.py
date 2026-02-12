import sys
import os
from shutil import copyfile

import yaml

# Wir importieren log_error/log_info zwar, nutzen sie aber nur da, wo es sicher ist
from plex_music_ratings_sync.logger import log_error, log_info
from plex_music_ratings_sync.util.paths import (
    get_config_dir,
    get_config_file_path,
    get_template_file_path,
)

_config = None
"""User configuration data."""


def _create_config(config_file_path):
    """Create a new configuration file from the template."""
    template_path = get_template_file_path()

    copyfile(template_path, config_file_path)


def init_config():
    """Initialize the configuration by checking Env Vars first, then falling back to YAML config file."""
    global _config

    # --- STRATEGY 1: Environment Variables (Docker Friendly) ---
    plex_url = os.getenv('PLEX_URL')
    plex_token = os.getenv('PLEX_TOKEN')
    plex_libraries = os.getenv('PLEX_LIBRARIES') # Expected format: "Music,Audiobooks"

    if plex_url and plex_token:
        # WICHTIG: Hier print() statt log_info() nutzen, da der Logger noch nicht existiert!
        print("INFO: Configuration detected in Environment Variables. Skipping config file.")
        
        # Parse libraries from comma-separated string to list
        libraries_list = []
        if plex_libraries:
            libraries_list = [lib.strip() for lib in plex_libraries.split(',') if lib.strip()]
        
        # Construct internal config structure manually
        _config = {
            "plex": {
                "url": plex_url,
                "token": plex_token,
                "libraries": libraries_list
            }
        }
        return
    # -----------------------------------------------------------

    # --- STRATEGY 2: YAML Config File (Legacy/Interactive) ---
    config_dir = get_config_dir()

    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)

    config_file_path = get_config_file_path()

    if not config_file_path.exists():
        _create_config(config_file_path)

    with open(config_file_path, "r") as config_file:
        _config = yaml.safe_load(config_file)


def get_plex_config():
    """Retrieve the Plex configuration."""
    if _config is None:
        # Hier ist print() auch sicherer, falls logger noch tot ist, aber log_error könnte gehen
        print("ERROR: Configuration not initialized.") 
        sys.exit(1)

    plex_config = _config.get("plex", {})

    if not isinstance(plex_config.get("url"), str) or not isinstance(
        plex_config.get("token"), str
    ):
        # Hier können wir log_error probieren, aber print ist sicherer beim Start
        print("ERROR: The Plex configuration is not valid")
        sys.exit(1)

    return plex_config