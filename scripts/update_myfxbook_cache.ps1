Set-Location "c:\Users\diale\OneDrive\Desktop\ProBot\news-api"
python scripts\fetch_myfxbook.py
python scripts\fetch_forexfactory.py
git add myfxbook_cache.json forexfactory_cache.json
$changes = git status --porcelain myfxbook_cache.json forexfactory_cache.json
if ($changes) {
    git commit -m "Update myfxbook + forexfactory cache" | Out-Null
    git push origin master | Out-Null
}
