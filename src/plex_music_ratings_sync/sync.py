import sys
import json
import os
import atexit
from datetime import datetime
from pathlib import Path

from plexapi.server import PlexServer

from plex_music_ratings_sync.config import get_plex_config
from plex_music_ratings_sync.logger import log_debug, log_error, log_info, log_warning
from plex_music_ratings_sync.ratings import (
    get_rating_from_file,
    get_rating_from_plex,
    set_rating_to_file,
    set_rating_to_plex,
)
from plex_music_ratings_sync.state import is_dry_run
from plex_music_ratings_sync.util.datetime import format_time

_SUPPORTED_EXTENSIONS = (".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aif", ".aiff")
"""Audio file extensions that are supported for rating synchronization."""

# --- CACHE CONFIGURATION START ---
# Determine the best location for the cache file
# Priority: 
# 1. CRON_CONFIG_DIR or CONFIG_DIR (Env Vars)
# 2. /config (Standard Docker)
# 3. /app/data (Current Container Image default)
# 4. Current working directory (Fallback)

search_paths = [
    os.getenv('CRON_CONFIG_DIR'),
    os.getenv('CONFIG_DIR'),
    '/config',
    '/app/data',
    '.'
]

config_path = Path('.')
for path in search_paths:
    if path and os.path.exists(path):
        config_path = Path(path)
        break

CACHE_FILE = config_path / 'rating_cache.json'
CACHE_SAVE_INTERVAL = 50 

_file_rating_cache = {}
_cache_dirty = False

def load_cache():
    global _file_rating_cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                _file_rating_cache = json.load(f)
                log_info(f"Loaded rating cache with {len(_file_rating_cache)} entries from {CACHE_FILE}")
        except Exception as e:
            log_error(f"Failed to load cache: {e}")
            _file_rating_cache = {}
    else:
        log_info(f"No cache found at {CACHE_FILE}, starting fresh.")

def save_cache():
    global _cache_dirty
    if _cache_dirty:
        try:
            # Ensure directory exists
            if not CACHE_FILE.parent.exists():
                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            with open(CACHE_FILE, 'w') as f:
                json.dump(_file_rating_cache, f)
            _cache_dirty = False
        except Exception as e:
            log_error(f"Failed to save cache: {e}")

# Save cache automatically when script exits
atexit.register(save_cache)
# --- CACHE CONFIGURATION END ---


class RatingSync:
    def __init__(self):
        # Load Cache
        load_cache()
        
        # List to store changes for the final summary
        self.updated_tracks = [] 

        plex_config = get_plex_config()

        try:
            log_info(f"Connecting to Plex server: **{plex_config['url']}**")

            self.plex = PlexServer(plex_config["url"], plex_config["token"])

            log_info(f"Connected to Plex server: **{self.plex.friendlyName}**")
        except Exception as e:
            log_error(f"Failed to connect to Plex server: {e}")
            sys.exit(1)

        self.libraries = plex_config["libraries"]

        if is_dry_run():
            log_warning("Running in dry-run mode (no changes will be made)")

    def _process_item(self, item, mode="sync"):
        """
        Process a single track with the specified mode.
        """
        global _file_rating_cache, _cache_dirty
        
        item_start_time = datetime.now()

        file_path = Path(item.media[0].parts[0].file)
        file_path_str = str(file_path)

        track_index = item.index if item.index is not None else 0

        # Standard log (level 3 = verbose/debug usually)
        log_info(
            f"Track: **{track_index:02d}. {item.title}** ({file_path.name})",
            3,
        )

        if not file_path.exists():
            log_warning("▸ File not found on disk", 4)
            return

        if file_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            log_warning("▸ Skipping unsupported file type", 4)
            return

        plex_rating = get_rating_from_plex(item)
        
        # --- CACHE LOGIC START ---
        try:
            current_mtime = file_path.stat().st_mtime
        except FileNotFoundError:
            return 

        file_rating = None
        
        # Check if file is in cache and valid
        cache_entry = _file_rating_cache.get(file_path_str)
        
        if cache_entry and cache_entry.get('mtime') == current_mtime:
            # CACHE HIT: Use cached rating
            file_rating = cache_entry.get('rating')
        else:
            # CACHE MISS: Read from file
            file_rating = get_rating_from_file(file_path_str)
            
            # Update cache
            _file_rating_cache[file_path_str] = {
                'mtime': current_mtime,
                'rating': file_rating
            }
            _cache_dirty = True
        # --- CACHE LOGIC END ---

        # Helper to track changes
        def track_change(action, old_val, new_val):
            msg = f"⚡ {action}: {item.title} ({old_val} -> {new_val})"
            log_warning(msg)
            self.updated_tracks.append(msg)

        if mode == "import" and file_rating is not None:
            if plex_rating != file_rating:
                track_change("IMPORT (File->Plex)", plex_rating, file_rating)
                set_rating_to_plex(item, file_rating)
            else:
                log_debug("▸ Plex rating already matches file", 4)
                
        elif mode == "export" and plex_rating is not None:
            if file_rating != plex_rating:
                track_change("EXPORT (Plex->File)", file_rating, plex_rating)
                set_rating_to_file(file_path_str, plex_rating)
                
                # Update Cache after Write
                try:
                    new_mtime = file_path.stat().st_mtime
                    _file_rating_cache[file_path_str] = {
                        'mtime': new_mtime,
                        'rating': plex_rating
                    }
                    _cache_dirty = True
                except Exception:
                    if file_path_str in _file_rating_cache:
                        del _file_rating_cache[file_path_str]

            else:
                log_debug("▸ File rating already matches Plex", 4)
                
        elif mode == "sync":
            if plex_rating != file_rating:
                if plex_rating is not None:
                    track_change("SYNC (Plex->File)", file_rating, plex_rating)
                    set_rating_to_file(file_path_str, plex_rating)
                    
                    # Update Cache after Write
                    try:
                        new_mtime = file_path.stat().st_mtime
                        _file_rating_cache[file_path_str] = {
                            'mtime': new_mtime,
                            'rating': plex_rating
                        }
                        _cache_dirty = True
                    except Exception:
                        if file_path_str in _file_rating_cache:
                            del _file_rating_cache[file_path_str]

                elif file_rating is not None:
                    track_change("SYNC (File->Plex)", plex_rating, file_rating)
                    set_rating_to_plex(item, file_rating)
            else:
                log_debug("▸ Ratings are already in sync", 4)

        item_elapsed_time = datetime.now() - item_start_time

        log_debug(f"▸ Processed in **{format_time(item_elapsed_time)}**", 4)

    def _process_libraries(self, mode="sync"):
        """Process all configured libraries with the specified mode."""
        total_start_time = datetime.now()
        processed_tracks = 0
        tracks_since_save = 0

        for library_name in self.libraries:
            log_info(f"Processing Plex library: **{library_name}**")

            try:
                library_section = self.plex.library.section(library_name)
            except Exception:
                log_error(f"Library not found: {library_name}")
                continue

            music_items = library_section.all()

            if not music_items:
                log_warning(f"No items found in library: **{library_name}**")
                continue

            for item in music_items:
                if hasattr(item, "type") and item.type == "artist":
                    # log_info(f"Artist: **{item.title}**", 1) # Optional: reduce log noise

                    for album in item.albums():
                        album_tracks = album.tracks()
                        if not album_tracks:
                            continue
                            
                        # album_path = Path(album_tracks[0].media[0].parts[0].file).parent
                        # log_info(f"Album: **{album.title}**", 2) # Optional: reduce log noise

                        for track in album_tracks:
                            self._process_item(track, mode=mode)
                            processed_tracks += 1
                            tracks_since_save += 1
                            
                            if tracks_since_save >= CACHE_SAVE_INTERVAL:
                                save_cache()
                                tracks_since_save = 0

        total_elapsed_item = datetime.now() - total_start_time

        log_info(
            f"Processed **{processed_tracks}** tracks in **{format_time(total_elapsed_item)}**"
        )
        
        save_cache()

    def _print_summary(self):
        """Prints a summary of all changes made during this run."""
        if self.updated_tracks:
            log_info("\n" + "="*50)
            log_info(f" SUMMARY: {len(self.updated_tracks)} TRACKS UPDATED")
            log_info("="*50)
            for msg in self.updated_tracks:
                log_info(msg)
            log_info("="*50 + "\n")
        else:
            log_info("\n" + "="*50)
            log_info(" SUMMARY: NO CHANGES WERE NEEDED")
            log_info("="*50 + "\n")

    def sync_ratings(self):
        """Synchronize ratings between Plex and supported audio files."""
        log_info("Synchronization started: **Plex ⇄ Audio Files**")
        self._process_libraries(mode="sync")
        self._print_summary()
        log_info("Synchronization completed: **Plex** ⇄ **Audio Files**")

    def import_ratings(self):
        """Import ratings from audio files into Plex."""
        log_info("Import started: **Audio Files → Plex**")
        self._process_libraries(mode="import")
        self._print_summary()
        log_info("Import completed: **Audio Files → Plex**")

    def export_ratings(self):
        """Export ratings from Plex to audio files."""
        log_info("Export started: **Plex → Audio Files**")
        self._process_libraries(mode="export")
        self._print_summary()
        log_info("Export completed: **Plex → Audio Files**")