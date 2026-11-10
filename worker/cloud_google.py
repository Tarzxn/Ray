"""Google web OAuth; tokens and verifier are stored only in the Codespace."""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode, urlparse
import requests
from google_tools import GoogleConnection, SCOPES


class CloudGoogle(GoogleConnection):
    def disconnect(self):
        self.config.vault.set('google_pending', None)
        return super().disconnect()

    def configure(self, data):
        client = data.get('web') if isinstance(data, dict) else None
        if not isinstance(client, dict) or not str(client.get('client_id', '')).endswith('.apps.googleusercontent.com'):
            raise ValueError('Import a Google OAuth Web application client JSON for Beam Cloud')
        old = self.config.vault.get('google_client')
        if old and old.get('client_id') != client['client_id']: self.config.vault.set('google_token', None)
        self.config.vault.set('google_client', {'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''), 'redirect_uris': client.get('redirect_uris', [])})
        return self.status()

    def start(self, redirect_uri=''):
        client = self.config.vault.get('google_client')
        if not client: raise ValueError('Import your Google Web application OAuth client first')
        parsed = urlparse(redirect_uri)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')): raise ValueError('Google callback must use HTTPS')
        if redirect_uri not in client.get('redirect_uris', []): raise ValueError('Add this exact callback to the Google Web client: ' + redirect_uri)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.config.vault.set('google_pending', {'state': state, 'verifier': verifier, 'redirect': redirect_uri, 'expires': time.time() + 600})
        self.auth = {'pending': True, 'error': ''}
        return {'pending': True, 'authorization_url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({'client_id': client['client_id'], 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': ' '.join(SCOPES), 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent'})}

    def finish(self, data):
        pending = self.config.vault.get('google_pending')
        if not pending or pending['expires'] < time.time() or not hmac.compare_digest(str(data.get('state', '')), pending['state']): raise ValueError('Google sign-in expired or did not match. Connect again.')
        self.config.vault.set('google_pending', None)
        self.auth = {'pending': False, 'error': ''}
        if data.get('error'): raise ValueError('Google sign-in was cancelled')
        client = self.config.vault.get('google_client')
        with requests.post('https://oauth2.googleapis.com/token', data={'client_id': client['client_id'], 'client_secret': client['client_secret'], 'code': data.get('code', ''), 'code_verifier': pending['verifier'], 'redirect_uri': pending['redirect'], 'grant_type': 'authorization_code'}, timeout=(10, 30)) as response:
            if response.status_code != 200: raise ValueError('Google could not complete sign-in. Check the Web client and callback URL.')
            token = response.json()
        if not token.get('access_token'): raise ValueError('Google returned no access token')
        token['expires_at'] = time.time() + int(token.get('expires_in', 3600)) - 60
        self.config.vault.set('google_token', token)
        return self.status()
