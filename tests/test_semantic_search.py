import asyncio

from api.semantic_search import SemanticIndex, SemanticSearchEngine


class _EmbeddingClient:
    def __init__(self):
        self.texts = []

    def embed(self, texts):
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class _PortalClient:
    def __init__(self):
        self.message_fetches = 0

    def nachrichten_get_headers(self, *_args):
        self.message_fetches += 1
        return {
            "success": True,
            "conversations": [
                {
                    "Id": "private-1",
                    "Betreff": "Private subject",
                    "Sender": "Teacher",
                    "Inhalt": "Private content",
                }
            ],
        }

    def meinunterricht_get_overview(self):
        return {
            "success": True,
            "entries": [{"entry_id": "course-1", "name": "Mathematik"}],
        }

    def kalender_get_events(self, *_args):
        return {"success": True, "events": []}

    def apps_get_modules(self):
        return {"success": True, "modules": []}


def test_message_free_semantic_index_never_fetches_or_embeds_messages() -> None:
    engine = SemanticSearchEngine()
    embeddings = _EmbeddingClient()
    portal = _PortalClient()
    engine._client = embeddings
    index = SemanticIndex("user-1")

    asyncio.run(engine._build_index("user-1", index, portal, include_messages=False))

    assert portal.message_fetches == 0
    assert all(
        "Private" not in text and "Teacher" not in text for text in embeddings.texts
    )
    assert set(index.documents) == {"sem-crs-course-1"}


def test_private_and_full_semantic_indices_are_separate() -> None:
    engine = SemanticSearchEngine()

    assert engine.get_index("user-1", include_messages=False) is not engine.get_index(
        "user-1", include_messages=True
    )
