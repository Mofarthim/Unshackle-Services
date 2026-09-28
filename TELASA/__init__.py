from __future__ import annotations

import json
import re
import sys
from hashlib import md5
from typing import Any, Optional, Union, List

import click
from click import Context
from langcodes import Language
from requests import Request

from unshackle.core.constants import AnyTrack
from unshackle.core.service import Service
from unshackle.core.manifests import HLS, DASH
from unshackle.core.titles import Title_T, Titles_T, Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapters, Tracks, Audio, Video, Subtitle
from unshackle.core.utils.collections import as_list

class TELASA(Service):
    """
    Service code for Telasa (https://www.telasa.jp/).
    \b
    Authorization: Cookies
    Security:
        Widevine:
            L3: 1080p (Possibly 2160p?)
        playReady:
            SL2000: 1080p
    Author: Crash@NSBC (Ported to Unshackle)
    """

    ALIASES = ("TLSA", "telasa")
    TITLE_RE = r"^(?:https?://(?:www\.)?telasa\.jp)/(?P<type>series|videos)/(?P<id>\d+)"

    @staticmethod
    @click.command(name="Telasa", short_help="https://www.telasa.jp/")
    @click.argument("title", type=str)
    @click.option("-s", "--single", is_flag=True, default=False, help="Download only the specific ID provided (do not fetch other seasons).")
    @click.option("-t", "--title", "force_title", type=str, default=None, help="Force a custom series/movie title.")
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> Telasa:
        return TELASA(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, single: bool, force_title: str | None = None):
        self.title_input = title
        super().__init__(ctx)
        
        self.single = single
        self.force_title = force_title

        parse = re.search(self.TITLE_RE, self.title_input)
        if not parse:
            self.title_id = self.title_input
            self.is_movie = True 
        else:
            self.title_id = parse.group("id")
            self.is_movie = bool(parse.group("type") == "videos")

        self.cdm = ctx.obj.cdm
        self.session.headers.update({
            "Origin": "https://www.telasa.jp",
            "Referer": "https://www.telasa.jp/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    def authenticate(self, cookies: Optional[Any] = None, credential: Optional[Any] = None) -> None:
        if not cookies:
            self.log.error("Service requires Cookies for Authentication.", exc_info=False)
            sys.exit(1)
            
        super().authenticate(cookies, credential)
        
        self.log.info("Logging into Telasa...")
        
        last_auth_user = next(
            (cookie.value for cookie in self.session.cookies if cookie.name.endswith("LastAuthUser")),
            None,
        )
        if not last_auth_user:
            self.log.error("- LastAuthUser not Found in cookies.", exc_info=False)
            import sys; sys.exit(1)
        
        prefix = "CognitoIdentityServiceProvider.71t3jfmis742ffmevo4677evms"
        access_token_cookie = next(
            (cookie for cookie in self.session.cookies if cookie.name == f"{prefix}.{last_auth_user}.accessToken"),
            None,
        )

        if not access_token_cookie:
            self.log.error("- Access Token not Found in cookies.", exc_info=False)
            import sys; sys.exit(1)

        access_token = access_token_cookie.value

        # Validate/Refresh Token logic
        req = self.session.post(
            url=self.config["endpoints"]["auth"],
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.GetUser",
                "X-Amz-User-Agent": "aws-amplify/5.0.4 auth framework/2",
            },
            json={"AccessToken": access_token},
        )

        data = req.json()

        if data.get("__type") == "NotAuthorizedException":
            self.log.info(" - Access Token expired, refreshing...")
            refresh_token = next(
                (cookie for cookie in self.session.cookies if cookie.name == f"{prefix}.{last_auth_user}.refreshToken"),
                None,
            )

            if not refresh_token:
                self.log.error("- Refresh Token not Found.", exc_info=False)
                import sys; sys.exit(1)

            req = self.session.post(
                url=self.config["endpoints"]["auth"],
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "x-amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                    "x-amz-User-Agent": "aws-amplify/5.0.4 auth framework/2",
                },
                json={
                    "ClientId": "71t3jfmis742ffmevo4677evms",
                    "AuthFlow": "REFRESH_TOKEN_AUTH",
                    "AuthParameters": {
                        "REFRESH_TOKEN": refresh_token.value,
                    },
                },
            )

            data = req.json()
            if data.get("__type") == "NotAuthorizedException":
                self.log.error(f" - Failed to Refresh Access Token -> {data.get('message')}", exc_info=False)
                import sys; sys.exit(1)

            access_token = data["AuthenticationResult"]["AccessToken"]

        device_id = next(
            iter(cookie for cookie in self.session.cookies if cookie.name == "did"),
            None,
        )

        if not device_id:
            self.log.error(" - Device ID (did) not Found in cookies.", exc_info=False)
            import sys; sys.exit(1)

        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "X-Device-Id": device_id.value,
        })
        self.log.info(" + Authentication Successful.")

    def get_titles(self) -> Titles_T:
        if self.is_movie:
            return self._get_movie_titles()
        else:
            return self._get_series_titles()

    def _get_movie_titles(self) -> Movies:
        req = self.session.get(self.config["endpoints"]["base"].format(id=self.title_id))
        data_match = re.search(r'<script id=\"__NEXT_DATA__" type=\"application/json\">(.+?)<\/script>', req.text)

        if not data_match:
            raise ValueError("Could not parse ID from Title.")

        data = json.loads(data_match.group(1).strip())
        video_detail = data["props"]["initialState"]["videoDetail"]["video"]
        series_detail = data["props"]["initialState"]["seriesDetail"]["series"]


        if series_detail is None:
            return Movies([
                Movie(
                    id_=video_detail["id"],
                    service=self.__class__,
                    name=self.force_title or video_detail["name"],
                    year=video_detail.get("year_of_production"),
                    data=video_detail
                )
            ])
        
        all_episodes = self._process_episodes(
            series_detail["episode_ids"], 
            self.force_title or series_detail["name"], 
            data.get("data", {}).get("season_sequence_number", 1)
        )

        if not self.single and "seasons" in series_detail:
            for season in series_detail["seasons"]:
                if str(season["series_id"]) == str(series_detail["id"]):
                    continue
                
                req_season = self.session.get(self.config["endpoints"]["series"].format(id=season["series_id"]))
                if req_season.ok:
                    s_data = req_season.json()
                    extra_eps = self._process_episodes(
                        s_data["data"]["episode_ids"], 
                        self.force_title or s_data["data"]["name"], 
                        s_data.get("data", {}).get("season_sequence_number")
                    )
                    all_episodes.extend(extra_eps)

        return Series(all_episodes)

    def _get_series_titles(self) -> Series:
        req = self.session.get(self.config["endpoints"]["series"].format(id=self.title_id))
        data = req.json()
        
        all_episodes = self._process_episodes(
            data["data"]["episode_ids"], 
            self.force_title or data["data"]["name"], 
            data.get("data", {}).get("season_sequence_number", 1)
        )

        if not self.single and "seasons" in data["data"]:
            for season in data["data"]["seasons"]:
                if str(season["series_id"]) == str(self.title_id):
                    continue
                
                req_season = self.session.get(self.config["endpoints"]["series"].format(id=season["series_id"]))
                if req_season.ok:
                    s_data = req_season.json()
                    extra_eps = self._process_episodes(
                        s_data["data"]["episode_ids"], 
                        self.force_title or s_data["data"]["name"], 
                        s_data.get("data", {}).get("season_sequence_number")
                    )
                    all_episodes.extend(extra_eps)

        return Series(all_episodes)

    def _process_episodes(self, video_ids: list, series_name: str, season_number: int) -> list[Episode]:
        req = self.session.post(
            url=self.config["endpoints"]["episode"],
            json={"video_ids": video_ids}
        )
        episodes_json = req.json()
        
        results = []
        skip_keywords = ["PR", "予告", "ダイジェスト", "特報", "解説放送"]

        for number, item in enumerate(episodes_json["data"]["items"]):
            episode_data = item.get("data", {})
            name = episode_data.get("name", "")
            subtitle = episode_data.get("subtitle", "")
            
            full_title_info = f"{name} {subtitle}"
            if any(keyword in full_title_info for keyword in skip_keywords):
                continue

            episode_number_match = re.search(r"第(\d+)話", name)
            ep_num = int(episode_number_match.group(1)) if episode_number_match else (number + 1)

            results.append(
                Episode(
                    id_=episode_data["id"],
                    service=self.__class__,
                    title=series_name,
                    season=season_number if season_number is not None else 1,
                    number=ep_num,
                    name=subtitle,
                    year=episode_data.get("year_of_production"),
                    data=episode_data
                )
            )
        return results

    def get_tracks(self, title: Title_T) -> Tracks:
        req = self.session.post(
            url=self.config["endpoints"]["graphql"],
            json={
                "query": f'{{ playbackToken( item_id: "{title.id}", item_type: Mezzanine ) {{ token expires_at license_id }} }}'
            },
        )
        data = req.json()
        if "errors" in data:
            self.log.error(f"Failed to Get Playback Token -> {data['errors'][0]['message']}", exc_info=False)
            import sys; sys.exit(1)

        playback_token = data["data"]["playbackToken"]["token"]

        req = self.session.post(
            url=self.config["endpoints"]["graphql"],
            headers={
                "X-Device-Id": self.session.headers.get("X-Device-Id", "979e52a4-d9aa-4dbb-b97b-f33434aa5aa7"),
            },
            json={
                "query": f'{{ manifests(item_id: "{title.id}", item_type: Mezzanine, playback_token: "{playback_token}") {{ protocol items {{ name url }} }} subtitles(id: "{title.id}", playback_token: "{playback_token}") {{ language url }} mezzanine(id: "{title.id}") {{ id title time {{ last_played duration endStart }} recommend {{ previous {{ id title images {{ url }} }} next {{ id title images {{ url }} }} }} video {{ id }} }} thumbnailSeekings(id: "{title.id}", playback_token: "{playback_token}") {{ quality url }} }}'
            },
        )
        data = req.json()
        
        manifest = next(
            (x for x in data["data"]["manifests"] if x["protocol"] == "dash"),
            None,
        )
        
        tracks = Tracks()
        
        if manifest["protocol"] == "hls":
            manifest_url = manifest["items"][0]["url"]
            tracks.add(HLS.from_url(manifest_url, self.session).to_tracks(language="ja"))
        else:
            manifest_url = manifest["items"][-1]["url"]
            tracks.add(DASH.from_url(manifest_url, self.session).to_tracks(language="ja"))


        for track in tracks:
            track.extra = {"playback_token": playback_token}
            if isinstance(track, Audio):
                track.language = Language.get("ja")

        if tracks.audio:
            tracks.audio.sort(key=lambda x: x.bitrate or 0, reverse=True)
            best_audio = tracks.audio[0]
            tracks.audio = [best_audio]

        if data.get("data", {}).get("subtitles"):
            for sub in data["data"]["subtitles"]:
                subtitle_url = sub.get("url")
                lang_code = sub.get("language", "ja")
                
                if not subtitle_url:
                    continue

                try:
                    cek = self.session.get(url=subtitle_url)
                    content_text = cek.text.strip()
                    if not cek.ok or not content_text.startswith("WEBVTT"):
                        continue
                except Exception as e:
                    continue

                track_id = md5(subtitle_url.encode()).hexdigest()[0:6]
                
                tracks.add(
                    Subtitle(
                        id_=track_id,
                        url=subtitle_url,
                        codec=Subtitle.Codec.WebVTT,
                        language=Language.get(lang_code),
                        forced=False,
                        sdh=False,
                    )
                )

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return []

    def get_widevine_service_certificate(self, **kwargs) -> Optional[bytes]:
        return None

    def get_widevine_license(self, challenge: bytes, track: AnyTrack, **kwargs) -> Optional[bytes]:
        try:
            token = track.extra.get("playback_token")
            if not token:
                self.log.error("License Error: 'playback_token' not found in track data.")
                return None

            req = self.session.post(
                url="https://license.kddi-video.com/",
                data=challenge,
                headers={
                    "X-Custom-Data": f"token_type=playback&token_value={token}&widevine_security_level=L3"
                },
            )
            
            if not req.ok:
                self.log.error(f"License Request Failed: HTTP {req.status_code}")
                self.log.debug(f"Server Response: {req.text}")
                return None
                
            return req.content
            
        except Exception as e:
            self.log.error(f"Exception during license request: {e}")
            return None

    def get_playready_license(self, challenge: bytes, track: AnyTrack, **kwargs) -> Optional[bytes]:
        try:
            token = track.extra.get("playback_token")
            if not token:
                return None

            req = self.session.post(
                url=self.config["endpoints"]["license"]["playready"],
                data=challenge,
                headers={
                    "X-Custom-Data": f"token_type=playback&token_value={token}&playready_security_level=SL2000"
                },
            )
            if not req.ok:
                return None
            return req.content
        except Exception:
            return None