from agentized_workflow.remote_identity import (
    commons_identity,
    gbif_identity,
    inaturalist_identity,
)


def test_inaturalist_photo_identity_does_not_depend_on_observation():
    first = inaturalist_identity(
        photo_id=91,
        observation_id=101,
        original_url="https://static.inaturalist.org/photos/91/original.jpg",
    )
    second = inaturalist_identity(
        photo_id=91,
        observation_id=102,
        original_url="https://static.inaturalist.org/photos/91/original.jpg",
    )

    assert first.source == "inaturalist"
    assert first.provider_asset_key == second.provider_asset_key == "photo:91"
    assert first.version_key == second.version_key == "photo:91"
    assert first.observation == {"observation_id": "101"}
    assert second.observation == {"observation_id": "102"}


def test_gbif_identity_normalizes_url_before_making_media_key():
    identity = gbif_identity(
        dataset_key="dataset-1",
        occurrence_key=42,
        media_identifier="HTTPS://images.example.org/fish.jpg?b=2&a=1#preview",
    )

    assert identity.source == "gbif"
    assert identity.provider_asset_key.startswith("dataset:dataset-1:occurrence:42:media:")
    assert identity.aliases[0].value == "https://images.example.org/fish.jpg?a=1&b=2"
    assert identity.aliases[0].immutable is False


def test_commons_identity_uses_file_sha1_as_immutable_version():
    identity = commons_identity(
        page_id=55,
        file_sha1="abc123",
        original_url="https://upload.wikimedia.org/wikipedia/commons/a/a1/example.jpg",
    )

    assert identity.source == "commons"
    assert identity.provider_asset_key == "page:55"
    assert identity.version_key == "sha1:abc123"
    assert identity.aliases[0].immutable is True
