import click
import json
import subprocess
import base64
import shutil
import os
import re
import binascii
from io import BytesIO
from http.cookiejar import CookieJar
from langcodes import Language
from typing import Any, Optional, Union
from uuid import UUID

from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T
from unshackle.core.tracks import Chapter, Chapters, Subtitle, Tracks, Audio, Video
from unshackle.core.manifests import DASH
from unshackle.core.credential import Credential
from unshackle.core.drm import Widevine
from pywidevine.pssh import PSSH

class AbemaTV(Service):
    """
    Service code for AbemaTV (https://abema.tv/).
    Authorization: Credentials / Cookies / Guest
    Security:
        Widevine:
            L3: 1080p 
    Ported from VT by Lucijan
    """

    ALIASES = ["ABMA", "ABEMA", "AbemaTV"]
    TITLE_RE = [
        r"video\/episode\/(?P<id>[0-9a-z-_]*)",
        r"slots\/(?P<id>[0-9a-zA-Z]*)",
        r"video\/title\/(?P<id>[0-9a-z-]*)"
    ]

    @staticmethod
    @click.command(name="AbemaTV", short_help="https://abema.tv/")
    @click.argument("title", type=str, required=True)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AbemaTV(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx=ctx)
        self.parse_title(ctx=ctx, title=title)
        self.original_title = title
        self.auth_data = None
        self.token = None 
        self.bearer_token = None
        self.video_type = "program"
        self.dtid = None
        self.credential = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        self.log.debug("Authenticating AbemaTV service...")
        self.credential = credential
        auth = super().authenticate(cookies, credential)
        self.configure()
        return auth

    def configure(self) -> None:
        self.log.info(" + Connecting to AbemaTV...")
        try:
            hmac_script_path = self._get_js_script_path('abemaHmac.js')
            guest_json = self._run_node_script(hmac_script_path, [])
            guest_login_url = self.config["endpoints"]["guest_login"]
            guest_res = self.session.post(url=guest_login_url, json=json.loads(guest_json)).json()
            
            if "token" not in guest_res:
                raise ConnectionError("Failed to generate Guest Token.")
            guest_token = guest_res["token"]
            
            username = None
            password = None
            if self.credential:
                username = self.credential.username
                password = self.credential.password
            if not username:
                username = self.config.get("username")
                password = self.config.get("password")

            if username and password:
                self.log.info(" + Credentials found. Attempting Premium Login...")
                auth_json = {"email": username, "password": password, "pageId": "account_management"}
                login_url = self.config["endpoints"]["login"]
                self.auth_data = self.session.post(url=login_url, json=auth_json, headers={"Authorization": f"bearer {guest_token}"}).json()
                if not self.auth_data.get("token"):
                    raise ConnectionError("Login failed.")
                self.bearer_token = self.auth_data["token"]
                self.log.info(" + Successfully logged in as User.")
            else:
                self.log.info(" + No credentials found. Using Guest Mode (Free Content Only).")
                self.bearer_token = guest_token
                self.auth_data = {"token": guest_token, "profile": {"userId": guest_res.get("profile", {}).get("userId", "guest_user")}}

            media_token_url = self.config["endpoints"]["media_token"]
            token_res = self.session.get(url=media_token_url, headers={"Authorization": f"bearer {self.bearer_token}"}).json()
            self.token = token_res["token"]

            dtid_url = self.config["endpoints"]["dtid"]
            dtid_res = self.session.get(url=dtid_url, headers={"Authorization": f"bearer {self.bearer_token}"}).json()
            self.dtid = dtid_res.get("deviceTypeId")

        except Exception as e:
            self.log.error(f" - AbemaTV authentication failed: {e}")
            raise ConnectionAbortedError("Could not authenticate with AbemaTV.")

    def get_titles(self) -> Title_T:
        titles = list()
        if not self.token: self.configure()
        current_id = self.title or self.original_title.split('/')[-1]
        is_series_page = "video/title/" in self.original_title
        is_slot_page = "slots/" in self.original_title
        headers = {"Authorization": f"bearer {self.bearer_token}"}

        if is_series_page:
            self.video_type = "program"
            series_info_url = self.config["endpoints"]["series_info"].format(id=current_id)
            series_data = self.session.get(url=series_info_url, headers=headers).json()
            for season in series_data.get("seasons", []):
                season_id = season["id"]
                programs_url = self.config["endpoints"]["series_programs"].format(series_id=current_id, season_id=season_id)
                programs_data = self.session.get(url=programs_url, headers=headers).json()
                for episode in programs_data.get("programs", []):
                    titles.append(Episode(id_=episode.get("id"), title=episode.get("series", {}).get("title"), name=episode.get("episode", {}).get("title"), season=episode.get("season", {}).get("sequence"), number=episode.get("episode", {}).get("number"), service=self.__class__, data=episode))
            return Series(titles)
        else:
            if is_slot_page:
                self.video_type = "slot"
                url = self.config["endpoints"]["slot_info"].format(id=current_id)
            else:
                self.video_type = "program"
                url = self.config["endpoints"]["program_info"].format(id=current_id)
            program_data = self.session.get(url=url, headers=headers).json()
            if self.video_type == "program":
                item = program_data
                titles.append(Episode(id_=item.get("id"), title=item.get("series", {}).get("title"), name=item.get("episode", {}).get("title"), season=item.get("season", {}).get("sequence"), number=item.get("episode", {}).get("number"), service=self.__class__, data=item))
            else:
                item = program_data.get("slot", {})
                titles.append(Movie(id_=item.get("id"), name=item.get("title"), year=None, service=self.__class__, data=item))
            if self.video_type == "slot": return Movies(titles)
            else: return Series(titles)

    def get_tracks(self, title: Title_T) -> Tracks:
        if self.video_type == "slot":
            manifest_url = self.config["endpoints"]["manifest_slot"].format(id=title.id, token=self.token)
        else:
            manifest_url = self.config["endpoints"]["manifest_program"].format(id=title.id, token=self.token, dtid=self.dtid)

        self.log.info(f" + Manifest: {manifest_url}")
        manifest_content = self.session.get(url=manifest_url).text
        tracks = DASH.from_text(manifest_content, manifest_url).to_tracks(language="ja")
        if tracks.audio:
            tracks.audio.sort(key=lambda x: x.bitrate or 0, reverse=True)
            best_audio = tracks.audio[0]
            tracks.audio = [best_audio]
        
        target_kid = None
        key_hex = None

        kid_match = re.search(r'default_KID="([0-9a-fA-F-]+)"', manifest_content, re.IGNORECASE)
        if kid_match:
            target_kid = kid_match.group(1)
            
        if not target_kid:
            video_track = next((t for t in tracks if isinstance(t, Video)), None)
            if video_track and video_track.url:
                try:
                    headers = {"Range": "bytes=0-4096"}
                    res = self.session.get(video_track.url, headers=headers)
                    if res.status_code in [200, 206]:
                        content = res.content
                        hex_content = binascii.hexlify(content).decode('utf-8')
                        wv_sys_id = "edef8ba979d64acea3c827dcd51d21ed"
                        sys_idx = hex_content.find(wv_sys_id)
                        
                        if sys_idx != -1:
                            box_start_hex = sys_idx - 24
                            if box_start_hex >= 0:
                                size_hex = hex_content[box_start_hex : box_start_hex+8]
                                try:
                                    box_size = int(size_hex, 16)
                                    box_data = content[int(box_start_hex/2) : int(box_start_hex/2)+box_size]
                                    pssh_obj = PSSH(box_data)
                                    for kid in pssh_obj.key_ids:
                                        target_kid = str(kid)
                                        break
                                except Exception:
                                    pass
                        
                        if not target_kid:
                            tenc_idx = hex_content.find("74656e63")
                            if tenc_idx != -1:
                                scan_start = tenc_idx + 8
                                scan_end = min(len(hex_content), scan_start + 100)
                                chunk = hex_content[scan_start:scan_end]
                                start = tenc_idx + 24
                                kid_hex = hex_content[start : start+32]
                                target_kid = f"{kid_hex[:8]}-{kid_hex[8:12]}-{kid_hex[12:16]}-{kid_hex[16:20]}-{kid_hex[20:]}"

                except Exception as e:
                    self.log.warning(f" - Probe failed: {e}")

        if target_kid:
            try:
                key_hex = self._get_key(program_id=title.id, kid_unconverted=str(target_kid))
            except Exception as e:
                self.log.error(f" - failed fetching key: {e}")
        else:
             self.log.warning(" ! KID not found. Content might be clear or probe failed.")

        if key_hex and target_kid:
            try:
                kid_uuid = UUID(str(target_kid))
                wv_system_id = UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")
                pssh_dummy = PSSH.new(key_ids=[kid_uuid], system_id=wv_system_id)
                
                wv_drm = Widevine(pssh=pssh_dummy, kid=kid_uuid)
                
                wv_drm.content_keys[kid_uuid] = key_hex
                
                for track in tracks:
                    if isinstance(track, (Video, Audio)):
                        track.drm = [wv_drm]
                        track.key = key_hex
                        track.extra = track.extra or {}
                        track.extra["key"] = key_hex
                        track.extra["kid"] = str(target_kid)
            
            except Exception as e:
                self.log.error(f" - Key Injection Error: {e}")

        for track in tracks:
            if isinstance(track, Audio): track.language = Language.get("ja")

        service_data = title.data or {}
        caption_groups = service_data.get("captionGroups", []) or service_data.get("episode", {}).get("captionGroups", [])
        if caption_groups:
            for caption in caption_groups:
                cap_uri = caption.get("uri")
                if not cap_uri: continue
                subtitle_url = f"https://ds-vod-abematv.akamaized.net{cap_uri}" if not cap_uri.startswith("http") else cap_uri
                lang_code = caption.get("language", "ja")
                tracks.add(Subtitle(id_=f"sub_{lang_code}", url=subtitle_url, codec=Subtitle.Codec.WebVTT, language=lang_code, forced=False, sdh=False))

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters([])
        
    def get_widevine_service_certificate(self, **_: Any) -> Optional[Union[str, bytes]]:
        return None
        
    def get_widevine_license(self, challenge: bytes, track: Tracks, **_: Any) -> Optional[Union[str, bytes]]:
        return None

    def _get_js_script_path(self, script_name):
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script_path = os.path.join(base_dir, 'javascript', script_name)
            if not os.path.exists(script_path): script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'javascript', script_name)
            return script_path
        except Exception: return os.path.join('javascript', script_name)

    def _run_node_script(self, script_path, args):
        potential_paths = [os.path.abspath('./binaries'), None, os.path.expandvars('%ProgramFiles%\\nodejs'), os.path.expandvars('%ProgramFiles(x86)%\\nodejs')]
        node_executable = None
        for path in potential_paths:
            node_executable = shutil.which("node", path=path) or shutil.which("node.exe", path=path)
            if node_executable:
                break
        if not node_executable:
            raise FileNotFoundError("Node.js not found.")
        command = [node_executable, script_path] + args
        return subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8').stdout.strip()

    def _get_key(self, program_id, kid_unconverted):
        try:
            if hasattr(kid_unconverted, 'hex'): kid_unconverted = kid_unconverted.hex
            kid_str = str(kid_unconverted).replace('-', '').lower()
            kid_b64 = self._convert_kid(kid_str)
            if not kid_b64:
                raise ValueError("Failed to convert KID.")
            license_req_json = {"kids": [kid_b64], "type": "temporary"}
            user_id = self.auth_data["profile"]["userId"] if self.auth_data and "profile" in self.auth_data else "guest_user"
            license_url = self.config["endpoints"]["license"].format(token=self.token, cid=program_id, ct=self.video_type)
            license_resp = self.session.post(url=license_url, json=license_req_json).text
            decrypt_script_path = self._get_js_script_path('abemaDecrypt.js')
            dec_json_str = self._run_node_script(decrypt_script_path, [license_resp, user_id])
            dec_json = json.loads(re.sub(r"(\b\w+\b):", r'"\1":', dec_json_str).replace("'", '"'))
            return self._decode_base64(dec_json["keys"][0]["k"]).hex()
        except Exception as e:
            self.log.error(f" - Failed to get key: {e}")
            raise

    def _convert_kid(self, kid):
        try:
            byte_array = bytearray.fromhex(kid.replace("-", ""))
            return base64.b64encode(byte_array).decode("utf-8").replace("=", "").replace("/", "_").replace("+", "-")
        except Exception: return None

    def _decode_base64(self, data):
        missing_padding = 4 - len(data) % 4
        if missing_padding: data += '=' * missing_padding
        return base64.urlsafe_b64decode(data)

    def parse_title(self, ctx, title):
        title = title or ctx.parent.params.get("title")
        if not title: return
        regexes = self.TITLE_RE if isinstance(self.TITLE_RE, list) else [self.TITLE_RE]
        for regex in regexes:
            m = re.search(regex, title)
            if m:
                self.title = m.group("id")
                return
        self.title = title