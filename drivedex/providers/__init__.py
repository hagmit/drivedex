from .base import (Cursor, Deleted, Item, NotConfigured, NotLoggedIn,  # noqa: F401
                   ResyncRequired, SourceSpec)


def get(name: str, config, store, account=None):
    if name == "ms":
        from .msgraph import GraphProvider as P
    elif name == "google":
        from .gdrive import DriveProvider as P
    else:
        raise ValueError(f"未対応のプロバイダです: {name}（ms / google）")
    return P(config.provider(name), store, account)
