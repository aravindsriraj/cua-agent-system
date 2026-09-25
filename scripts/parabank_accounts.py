"""Print the current account numbers of ParaBank's demo customer john. They change whenever ParaBank resets its data.

  uv run python scripts/parabank_accounts.py      # e.g. 13344 14232 14343
"""
import re
import urllib.request

API = "https://parabank.parasoft.com/parabank/services/bank"


def accounts(user: str = "john", password: str = "demo") -> list[str]:
    def get(path: str) -> str:
        return urllib.request.urlopen(API + path, timeout=30).read().decode()

    customer = re.search(r"<id>(\d+)</id>", get(f"/login/{user}/{password}"))[1]
    return re.findall(r"<account><id>(\d+)</id>", get(f"/customers/{customer}/accounts"))


if __name__ == "__main__":
    print(*accounts())
