with open('main.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_func = '''    def open_download(self, url: str, start: int = 0) -> requests.Response:
        """Open a streaming (optionally ranged) download. Caller must close()."""
        headers = {"Range": f"bytes={start}-"} if start > 0 else None
        resp = self._request("GET", url, headers=headers, stream=True)
        if start > 0 and resp.status_code != 206:
            resp.close()
            raise ZoomError("Zoom did not honour the Range request; cannot resume the download.")
        return resp'''

new_func = '''    def open_download(self, url: str, start: int = 0) -> requests.Response:
        """Open a streaming (optionally ranged) download. Caller must close()."""
        token = self._get_token()
        sep = "&" if "?" in url else "?"
        download_url = f"{url}{sep}access_token={token}"
        headers = {"Range": f"bytes={start}-"} if start > 0 else {}
        try:
            resp = requests.get(
                download_url,
                headers=headers,
                stream=True,
                timeout=(10, config.HTTP_TIMEOUT_SECONDS),
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise ZoomError(f"Failed to start Zoom download: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:300]
            resp.close()
            raise ZoomError(f"Zoom download error HTTP {resp.status_code}: {body}")
        if start > 0 and resp.status_code != 206:
            resp.close()
            raise ZoomError("Zoom did not honour the Range request; cannot resume the download.")
        return resp'''

if old_func in text:
    text = text.replace(old_func, new_func)
    with open('main.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Successfully updated main.py!")
else:
    print("Could not find the function match in main.py.")
