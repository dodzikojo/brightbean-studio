from django.urls import reverse


def test_privacy_policy_is_public_and_owned_by_issuelab(client):
    response = client.get(reverse("privacy_policy"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "IssueLab Limited" in body
    assert "Company number 12951099" in body
    assert "LinkedIn data" in body
    assert "tech@issuelab.co" in body
    assert "InnovationCraft" not in body


def test_signup_links_to_local_privacy_policy(client, db):
    response = client.get(reverse("account_signup"))

    assert response.status_code == 200
    assert 'href="/privacy-policy/"' in response.content.decode()
