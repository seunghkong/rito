import datetime
import json
import os
import os.path
from itertools import cycle, zip_longest

import requests
from backoff import expo, on_exception
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from ratelimit import RateLimitException
from requests import exceptions

from utils import deep_get

# google API vals
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
START_ROW = 3
RANGE_NAME = f"회원정보!A{START_ROW}:C"
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# riot API vals
# see https://developer.riotgames.com/
RIOT_API_KEY = os.environ["RIOT_API_KEY"]  # masked
PLATFORM_LIST = ["americas", "asia", "europe"]
def riot_platform_gen():
    for platform in cycle(PLATFORM_LIST):
        yield f"https://{platform}.api.riotgames.com"
REGION_LIST = ["br1", "eun1", "euw1", "jp1", "kr", "la1","la2","na1","oc1","ph2","ru","sg2","th2","tr1","tw2", "vn2"]
def riot_region_gen():
    # for region in cycle(REGION_LIST):
    return "https://kr.api.riotgames.com"
RIOT_PLATFORM_ROUTE = riot_platform_gen()
RIOT_REGIONAL_ROUTE = riot_region_gen()
# defaultratelimit
RLIMIT = 100
RTIME = 120
# EX_PUUID = "0sOaIeiINDLkTejcsehuS1518HAwi3dWl5SlCa7jK7QmxUQj8n2jeklBYCtcSy6ujUL_ioM3bxJMpg"


def champion_mapper(version) -> dict:
    file_name = f"champion_{version}.json"
    raw_champions: dict
    if not os.path.isfile(file_name):
        champion_list = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json",
            timeout=5,
        )
        champion_list.raise_for_status()
        raw_champions = champion_list.json()
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(raw_champions, f)
    else:
        with open(file_name, "r", encoding="utf-8") as f:
            raw_champions = json.load(f)
    champion_data: dict = raw_champions["data"]
    return {
        int(champion_meta["key"]): champion_name
        for champion_name, champion_meta in champion_data.items()
    }
CHAMPION_MAPPER=champion_mapper("14.1.1")

class RiotUser:
    uid: str
    name: str
    id: str
    tag: str
    puuid: str = ""
    summoner_id: str
    tier: dict
    champions: list
    last_played: str

    def __init__(self, sheet_row: list[str]):
        self.uid = sheet_row[0]
        self.name = sheet_row[1]
        id_tag: list[str] = sheet_row[2].split("#")
        self.id = id_tag[0]
        try:
            self.tag = id_tag[1]
        except IndexError:
            self.tag = "KR1"
        self.puuid = self._get_puuid()
        self.summoner_id = self._get_summoners_id()
        self.champions = self._get_top_champs()
        self.tier = self._get_tier()
        self.last_played = self._get_recent_match_time()

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_puuid(self):
        response: requests.Response = requests.get(
            f"{next(RIOT_PLATFORM_ROUTE)}/riot/account/v1/accounts/by-riot-id/{self.id}/{self.tag}",
            timeout=10,
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: dict = response.json()
            return resp_json["puuid"]
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            raise ValueError(
                f"user {self.name}: {self.id}#{self.tag} puuid not found ({e}). Skipping.."
            ) from e
        except KeyError as e:
            raise KeyError(
                f"user {self.name}: {self.id}#{self.tag} puuid not found ({e}). Skipping.."
            ) from e

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_summoners_id(self):
        response: requests.Response = requests.get(
            f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{self.puuid}",
            timeout=5,
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: dict = response.json()
            return resp_json["id"]
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            raise ValueError(
                f"user {self.name}: {self.id}#{self.tag} summoner id not found ({e}). Skipping.."
            ) from e
        except KeyError as e:
            raise KeyError(
                f"user {self.name}: {self.id}#{self.tag} summoner id not found ({e}). Skipping.."
            ) from e

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_recent_match(self, match_type="", count: int = 1) -> str:
        response: requests.Response = requests.get(
            f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{self.puuid}/ids",
            params={"type": match_type, "count": count},
            timeout=5,
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: list[str] = response.json()
            if len(resp_json) < 1:
                raise ValueError("no recent matches found.")
            return resp_json[0]
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            raise ValueError(f"user {self.name}: {e}") from e
        except ValueError as e:
            raise ValueError(f"user {self.name}: {e}. Skipping..") from e

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_recent_match_time(self) -> str:
        recent_match = self._get_recent_match()
        response: requests.Response = requests.get(
            f"https://asia.api.riotgames.com/lol/match/v5/matches/{recent_match}",
            timeout=5,
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: dict = response.json()
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            print(f"user {self.name}: match {recent_match} not found. error: {e}")
        game_creation = deep_get(resp_json, "info.gameCreation", "0")
        return (
            datetime.datetime.fromtimestamp(int(game_creation) // 1000)
            .astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            .strftime("%Y-%m-%d %H:%M:%S")
        )

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_top_champs(self, count:int=3) -> list:
        response: requests.Response = requests.get(
            f"https://kr.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{self.puuid}/top",
            timeout=5,
            params={"count": count},
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: list[dict] = response.json()
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            print(f"user {self.name}: top champion list not found. error: {e}")
            return []
        return [CHAMPION_MAPPER.get(champ_mastery_dto.get("championId"), "")
                for champ_mastery_dto in resp_json]

    @on_exception(expo, RateLimitException, max_tries=10)
    def _get_tier(self) -> dict:
        response: requests.Response = requests.get(
            f"{RIOT_REGIONAL_ROUTE}/lol/league/v4/entries/by-summoner/{self.summoner_id}",
            timeout=5,
            headers={"X-Riot-Token": RIOT_API_KEY},
        )
        try:
            response.raise_for_status()
            resp_json: list[dict] = response.json()
            if len(resp_json) < 1:
                return {}
            return {
                league["queueType"]: {"tier": f"{league["tier"]}{league["rank"]}",
                                      "winloss": round(league["wins"]/(league["wins"]
                                                       + league["losses"]), 3) * 100,
                                      "inactive": league["inactive"]}
                for league in resp_json
            }
        except exceptions.HTTPError as e:
            if response.status_code == 429:
                retry_after = response.headers["Retry-After"]
                print(f"Ratelimit reached. Wait {retry_after} seconds..")
                raise RateLimitException("ratelimited", retry_after) from e
            print(f"user {self.name}: tier not found. error: {e}")
            return {}

    def __str__(self) -> str:
        return f"user {self.name}: {self.id}#{self.tag} of puuid {self.puuid} last played in {self.last_played}"


def main():
    credentials = service_account.Credentials.from_service_account_file(
        "credentials.json", scopes=SCOPES
    )
    try:
        credentials.refresh(Request())
        # session = AuthorizedSession(credentials)
        service = discovery.build(
            "sheets", "v4", credentials=credentials
        )
        print(f"{credentials.service_account_email} successfully connected.")
        # Call the Sheets API
        sheet = service.spreadsheets() # pylint: disable=no-member
        result: list[str] = (
            sheet.values()
            .get(spreadsheetId=GOOGLE_SHEET_ID, range=RANGE_NAME, majorDimension="ROWS")
            .execute()
        )
        values = result.get("values", [])

        if not values:
            print("No data found.")
            return

        for i, row in enumerate(values):
            if len(row) < 3:
                continue
            current_row = i+START_ROW
            try:
                usr = RiotUser(row)
                print(usr)
            except Exception as e:  # pylint: disable=W0718
                print(e)
                continue
            new_values = []
            for col in ["RANKED_SOLO_5x5", "RANKED_FLEX_SR"]:
                if not usr.tier.get(col):
                    new_values.extend([""]*2)
                else:
                    new_values.extend([deep_get(usr.tier, f"{col}.tier"),
                                       deep_get(usr.tier, f"{col}.winloss")])
            for _, champ in zip_longest(range(3), usr.champions, fillvalue=""):
                new_values.append(champ)
            new_values.append(usr.last_played)
            (
                sheet.values()
                .update(
                    spreadsheetId=GOOGLE_SHEET_ID, range=f"회원정보!L{current_row}:S{current_row}",
                    body={
                        "values": [new_values]
                    },
                    valueInputOption="RAW"
                ).execute()
            )

    except HttpError as err:
        print(err)


if __name__ == "__main__":
    main()
