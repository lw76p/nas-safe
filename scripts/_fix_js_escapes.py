import io

p = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone\web\app.js"
raw = io.open(p, "r", encoding="utf-8", newline="").read()
crlf = raw.count("\r\n") * 2 > raw.count("\n")
s = raw.replace("\r\n", "\n")
s = s.replace("\\`", "`").replace("\\${", "${")
if crlf:
    s = s.replace("\n", "\r\n")
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("fixed")
