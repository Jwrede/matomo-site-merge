"""Click through Matomo's web installer (database, super user, first site).

    python3 install.py http://127.0.0.1:18080
"""
import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

base = sys.argv[1].rstrip("/")
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(action, form=None):
    url = f"{base}/index.php?action={action}"
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    with opener.open(url, data, timeout=300) as r:
        return r.read().decode("utf-8", "replace")


def hidden(html):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)"[^>]*value="([^"]*)"', html))


call("welcome")
call("systemCheck")
html = call("databaseSetup")
form = {**hidden(html), "host": "127.0.0.1:13306", "username": "matomo", "password": "matomo",
        "dbname": "matomo", "tables_prefix": "matomo_", "adapter": "PDO\\MYSQL",
        "schema": "Mysql", "submit": "Next »"}
html = call("databaseSetup", form)
if "tablesCreation" not in html and "setupSuperUser" not in html:
    sys.exit("database setup failed:\n" + re.sub(r"<[^>]+>", " ", html)[:2000])
call("tablesCreation")
html = call("setupSuperUser")
form = {**hidden(html), "login": "admin", "password": "admin-pass-1234",
        "password_bis": "admin-pass-1234", "email": "admin@example.org", "submit": "Next »"}
html = call("setupSuperUser", form)
html = call("firstWebsiteSetup")
form = {**hidden(html), "siteName": "Old site", "url": "https://old.example.org",
        "timezone": "UTC", "ecommerce": "0", "submit": "Next »"}
html = call("firstWebsiteSetup", form)
try:
    call("trackingCode")  # only shows the JS snippet; has answered 500 under php -S
except urllib.error.HTTPError:
    pass
html = call("finished")
form = {**hidden(html), "do_not_track": "0", "anonymise_ip": "0", "submit": "Continue to Matomo »"}
call("finished", form)
print("installed")
