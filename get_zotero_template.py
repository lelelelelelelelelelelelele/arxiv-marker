import urllib.request
try:
    print(urllib.request.urlopen("https://raw.githubusercontent.com/zotero/make-it-red/master/manifest.json").read().decode('utf-8'))
except Exception as e:
    print(e)
