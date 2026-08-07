import json
p = r"C:\Users\deped\AppData\Local\Google\Chrome\User Data\Local State"
d = json.load(open(p, encoding="utf-8"))
print("last_used:", d.get("profile", {}).get("last_used"))
info = d.get("profile", {}).get("info_cache", {})
for name, i in info.items():
    print(f"  {name}: {i.get('user_name','')}")
