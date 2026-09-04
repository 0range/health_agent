from __future__ import annotations

import pytest

from health_agent.google_drive.config import DriveProfile, normalize_folder_id

PROFILE_ID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB", "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"),
        (
            "https://drive.google.com/drive/folders/1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
            "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
        ),
        (
            "https://drive.google.com/open?id=1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
            "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
        ),
    ),
)
def test_normalizes_supported_folder_references(value: str, expected: str) -> None:
    assert normalize_folder_id(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        "https://example.com/drive/folders/1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
        "https://docs.google.com/document/d/1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
        "../../other-profile",
        "short",
    ),
)
def test_rejects_non_folder_or_unsafe_references(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_folder_id(value)


def test_profile_deduplicates_roots_and_validates_local_key() -> None:
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    profile = DriveProfile.create(PROFILE_ID, [folder, folder])
    assert profile.root_folder_ids == (folder,)

    with pytest.raises(ValueError):
        DriveProfile.create("../vitalii", [folder])
