@echo off
echo === OPTIONS ===
curl -s -L -X OPTIONS --url "https://api.xemshort.top/allepisode?shortPlayId=2042069267620298754" -H "accept: */*" -H "accept-language: en-AU,en;q=0.9,vi;q=0.8,fr-FR;q=0.7,fr;q=0.6,en-US;q=0.5,hi;q=0.4,ar;q=0.3" -H "access-control-request-headers: short-source" -H "access-control-request-method: GET" -H "origin: https://xemshort.top" -H "priority: u=1, i" -H "referer: https://xemshort.top/" -H "sec-fetch-dest: empty" -H "sec-fetch-mode: cors" -H "sec-fetch-site: same-site" -H "user-agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"

echo.
echo === GET ===
curl -s -L --url "https://api.xemshort.top/allepisode?shortPlayId=2042069267620298754" -H "accept: */*" -H "accept-language: vi,en;q=0.9" -H "origin: https://xemshort.top" -H "referer: https://xemshort.top/" -H "short-source: web" -H "user-agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
