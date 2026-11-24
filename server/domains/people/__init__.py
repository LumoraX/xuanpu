"""纯个人空间「人物与关系（People）」领域包。"""

from __future__ import annotations

from server.domains.people.models import (
    ALLOWED_AVATAR_MIMES,
    MAX_AVATAR_BYTES,
    MAX_NOTES,
    MAX_PEOPLE,
    MAX_RELATIONS,
    SYMMETRIC_TYPES,
    AvatarMeta,
    BirthInfo,
    ContactEntry,
    MergeInput,
    NoteData,
    PeopleBundleInput,
    PersonProfile,
    RelationData,
    UnmergeInput,
)
from server.domains.people.service import PeopleService
from server.domains.people.store import PeopleStore

__all__ = [
    "ALLOWED_AVATAR_MIMES",
    "AvatarMeta",
    "BirthInfo",
    "ContactEntry",
    "MAX_AVATAR_BYTES",
    "MAX_NOTES",
    "MAX_PEOPLE",
    "MAX_RELATIONS",
    "MergeInput",
    "NoteData",
    "PeopleBundleInput",
    "PeopleService",
    "PeopleStore",
    "PersonProfile",
    "RelationData",
    "SYMMETRIC_TYPES",
    "UnmergeInput",
]
