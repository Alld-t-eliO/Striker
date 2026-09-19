import subprocess
import tempfile
import os

url = input("URL à tester : ").strip()
max_users = int(input("Nombre max d'utilisateurs simultanés : "))
step = int(input("Augmentation par palier : "))
stage_duration = input("Durée de chaque palier (ex: 20s, 1m) : ").strip()
sleep_time = float(input("Pause entre requêtes par utilisateur (secondes, ex: 0.1) : "))

if not url.startswith(("http://", "https://")):
    raise ValueError("L'URL doit commencer par http:// ou https://")

if max_users < 1:
    raise ValueError("max_users doit être supérieur à 0.")

if step < 1:
    raise ValueError("step doit être supérieur à 0.")

if sleep_time < 0:
    raise ValueError("La pause ne peut pas être négative.")


targets = list(range(step, max_users + 1, step))

if targets[-1] != max_users:
    targets.append(max_users)


stages = ""

for target in targets:
    stages += f"""
        {{ duration: '{stage_duration}', target: {target} }},"""

stages += """
        { duration: '10s', target: 0 },"""


k6_script = f"""
import http from 'k6/http';
import {{ sleep }} from 'k6';

export const options = {{
    stages: [
        {stages}
    ],

    thresholds: {{
        http_req_failed: ['rate<0.05'],
        http_req_duration: ['p(95)<2000'],
    }},
}};

export default function () {{
    const response = http.get('{url}');

    if (response.status >= 500) {{
        console.log(`HTTP ${{response.status}}`);
    }}

    sleep({sleep_time});
}}
"""


with tempfile.NamedTemporaryFile(
    mode="w",
    suffix=".js",
    delete=False
) as temp_file:

    temp_file.write(k6_script)
    script_path = temp_file.name


print("\n========== TEST ==========")
print(f"URL             : {url}")
print(f"Utilisateurs max: {max_users}")
print(f"Palier          : +{step}")
print(f"Durée/palier    : {stage_duration}")
print(f"Pause utilisateur: {sleep_time}s")
print("==========================\n")


try:
    subprocess.run(
        ["k6", "run", script_path],
        check=False
    )

except FileNotFoundError:
    print(
        "\n[ERROR] k6 n'est pas installé.\n"
        "Sur macOS : brew install k6"
    )

except KeyboardInterrupt:
    print("\n[STOP] Test interrompu.")

finally:
    os.remove(script_path)
