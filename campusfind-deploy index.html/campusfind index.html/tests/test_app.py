"""Run with: .venv/bin/python -m pytest -q"""
import io
from datetime import date

import pytest
from PIL import Image

import app as site


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(site, "DATA_DIR", tmp_path)
    monkeypatch.setattr(site, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(site, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(site, "DEMO_MODE", True)
    site.UPLOAD_DIR.mkdir()
    site.init_db()
    site._recent_posts.clear()
    site._recent_messages.clear()
    return site.app.test_client()


def post_data(**kwargs):
    base = {"type": "found", "title": "Striped canvas pencil case", "category": "Other",
            "location": "North Library", "description": "A green striped pencil case was left by the notice board.",
            "contactEmail": "", "date": date.today().isoformat()}
    base.update(kwargs)
    return base


def test_health_and_fictional_demo_filtering(client):
    assert client.get('/health').json == {"status": "ok"}
    assert client.get('/api/config').json["demo"] is True
    data = client.get('/api/items').json
    assert data["count"] == 6
    assert data["stats"] == {"found": 4, "lost": 2, "reunited": 0}
    found = client.get('/api/items?type=found&category=Bottles&q=library').json
    assert found["count"] == 1
    assert found["items"][0]["sample"] is True
    assert client.get('/api/items?category=not-a-category').status_code == 400
    assert client.get('/api/items?q=%25%27%20OR%201%3D1').json["count"] == 0
    detail = client.get('/api/items/demo-1').json
    assert detail["matches"][0]["id"] == 'demo-5'
    assert detail["item"]["contactEmail"] == ""


def test_create_edit_messages_reunite_delete_and_authorisation(client):
    r = client.post('/api/items', data=post_data(contactEmail='finder@example.edu'))
    assert r.status_code == 201, r.json
    item, token = r.json['item'], r.json['manageToken']
    item_id = item['id']
    assert item['sample'] is False
    assert len(token) == 64
    assert 'manageToken' not in client.get('/api/items/'+item_id).json['item']
    assert 'owner_hash' not in client.get('/api/items').json['items'][0]
    assert client.patch('/api/items/'+item_id, data=post_data(title='New title')).status_code == 403
    assert client.get('/api/items/'+item_id+'/messages').status_code == 403
    assert client.post('/api/items/'+item_id+'/resolve').status_code == 403
    assert client.delete('/api/items/'+item_id).status_code == 403
    assert client.post('/api/items/demo-1/messages', json={'replyEmail':'a@b.edu','message':'This is a real message.'}).status_code == 404
    sent = client.post('/api/items/'+item_id+'/messages', json={'replyEmail':'owner@example.edu','message':'I think this is mine. I can describe a private mark.'})
    assert sent.status_code == 201, sent.json
    messages = client.get('/api/items/'+item_id+'/messages', headers={'X-Manage-Token':token}).json['messages']
    assert len(messages) == 1 and messages[0]['replyEmail'] == 'owner@example.edu'
    updated = client.patch('/api/items/'+item_id, data=post_data(title='Striped green case'), headers={'X-Manage-Token':token})
    assert updated.status_code == 200, updated.json
    assert updated.json['item']['title'] == 'Striped green case'
    resolved = client.post('/api/items/'+item_id+'/resolve', headers={'X-Manage-Token':token})
    assert resolved.status_code == 200
    assert resolved.json['item']['status'] == 'reunited'
    assert resolved.json['item']['contactEmail'] == ''
    assert client.get('/api/items').json['stats']['reunited'] == 1
    assert item_id not in [i['id'] for i in client.get('/api/items').json['items']]
    assert client.delete('/api/items/'+item_id, headers={'X-Manage-Token':token}).status_code == 200
    assert client.get('/api/items/'+item_id).status_code == 404
    assert client.get('/api/items/'+item_id+'/messages', headers={'X-Manage-Token':token}).status_code == 404


def test_validation_and_image_processing(client):
    assert client.post('/api/items', data=post_data(title='x')).status_code == 400
    assert client.post('/api/items', data=post_data(category='nope')).status_code == 400
    assert client.post('/api/items', data=post_data(date='20260924')).status_code == 400
    assert client.post('/api/items', data=post_data(contactEmail='not-an-email')).status_code == 400
    picture=Image.new('RGBA',(140,90),(30,180,110,130))
    stream=io.BytesIO();picture.save(stream,format='PNG');stream.seek(0)
    payload=post_data(photo=(stream,'item.png','image/png'))
    r=client.post('/api/items', data=payload, content_type='multipart/form-data')
    assert r.status_code == 201, r.json
    filename=r.json['item']['photo'].split('/')[-1]
    image=client.get('/uploads/'+filename)
    assert image.status_code == 200 and image.mimetype == 'image/jpeg'
    with Image.open(io.BytesIO(image.data)) as processed:
        assert processed.mode == 'RGB' and processed.size == (140,90)
        assert 'exif' not in processed.info
    assert client.get('/uploads/../../app.py').status_code in (400,404)


def test_real_mode_hides_demo_posts(client, monkeypatch):
    monkeypatch.setattr(site,'DEMO_MODE',False)
    r=client.get('/api/items').json
    assert r['items'] == []
    assert r['stats'] == {"found": 0, "lost": 0, "reunited": 0}
    assert client.get('/api/items/demo-1').status_code == 404
