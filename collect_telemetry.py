#!/usr/bin/env python3
"""Снятие телеметрии ноутбука с загрузочной флешки (живая Linux-система): пример к эпизоду 53, не проверялся на реальных ноутбуках.

Только чтение: скрипт ничего не записывает на диски ноутбука, не снимает и не обходит блокировки. Он собирает данные
(износ батареи, состояние SSD, серийники, признаки корпоративной привязки) и по желанию отправляет их на ваш n8n.

Запуск (от root на живой Linux-системе; нужны dmidecode и smartctl из smartmontools; hivexget из libguestfs-tools или hivex
по желанию, для офлайн-поиска следов MDM и Autopilot в реестре Windows):
    sudo python3 collect_telemetry.py --sticker СЕРИЙНИК_С_НАКЛЕЙКИ --dry-run
    sudo python3 collect_telemetry.py --sticker СЕРИЙНИК_С_НАКЛЕЙКИ --url https://ВАШ_ДОМЕН/webhook/laptop-intake --token ТОКЕН

Честно об ограничениях:
 - Признаки MDM и Autopilot ищутся по следам в офлайн-реестре Windows (если системный раздел не стёрт) и по признакам прошивки.
   Если Windows стёрта, следов в реестре нет: тогда флаги будут «не проверены» (None), а не «чисто». Это не гарантия.
 - Серийник на наклейке вводится вручную; сравнивается с серийником системы, прошитым в плату (SMBIOS).
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import urllib.request

SYS = os.environ.get("SYS_ROOT", "/sys")


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def battery():
    """Износ батареи: 100 минус отношение полной ёмкости к проектной, в процентах."""
    for bat in sorted(glob.glob(f"{SYS}/class/power_supply/BAT*")):
        full = read(f"{bat}/energy_full") or read(f"{bat}/charge_full")
        design = read(f"{bat}/energy_full_design") or read(f"{bat}/charge_full_design")
        cycles = read(f"{bat}/cycle_count")
        if full and design and design.isdigit() and full.isdigit() and int(design) > 0:
            wear = round(max(0.0, 100.0 - 100.0 * int(full) / int(design)), 1)
            return {"present": True, "wear_pct": wear, "cycles": int(cycles) if cycles and cycles.isdigit() else None}
    return {"present": False, "wear_pct": None, "cycles": None}


def ssd(dev=None):
    """Состояние накопителя по SMART: для NVMe 100 минус percentage_used, для SATA признак PASSED и переназначенные сектора."""
    devs = [dev] if dev else sorted(glob.glob("/dev/nvme?n1")) + sorted(glob.glob("/dev/sd?"))
    for d in devs:
        out = run(["smartctl", "-j", "-a", d])
        if not out:
            continue
        try:
            j = json.loads(out)
        except ValueError:
            continue
        nv = j.get("nvme_smart_health_information_log")
        passed = (j.get("smart_status") or {}).get("passed")
        if nv and nv.get("percentage_used") is not None:
            return {"dev": d, "kind": "nvme", "health_pct": max(0, 100 - int(nv["percentage_used"])), "passed": passed, "power_on_hours": nv.get("power_on_hours")}
        realloc = None
        for a in (j.get("ata_smart_attributes") or {}).get("table", []):
            if a.get("id") == 5:
                realloc = (a.get("raw") or {}).get("value")
        health = 100 if passed and not realloc else (70 if passed else 20)
        return {"dev": d, "kind": "sata", "health_pct": health, "passed": passed, "reallocated": realloc, "power_on_hours": (j.get("power_on_time") or {}).get("hours")}
    return {"dev": None, "kind": None, "health_pct": None, "passed": None}


def dmi(key):
    out = run(["dmidecode", "-s", key])
    return out.strip().splitlines()[0].strip() if out and out.strip() else None


def windows_hives():
    """Ищет смонтированные разделы с Windows (только для чтения, монтирование выполняет оператор: mount -o ro)."""
    return glob.glob("/mnt/*/Windows/System32/config/SOFTWARE") + glob.glob("/media/*/*/Windows/System32/config/SOFTWARE")


def hive_flags():
    """Следы MDM и Autopilot в офлайн-реестре Windows. None = не проверено (нет раздела Windows или нет инструмента hivexget)."""
    hives = windows_hives()
    if not hives:
        return {"checked": False, "mdm_enrollment_keys": None, "autopilot_keys": None, "reason": "раздел Windows не найден (диск стёрт или не смонтирован)"}
    if not run(["hivexget", "--help"]):
        return {"checked": False, "mdm_enrollment_keys": None, "autopilot_keys": None, "reason": "нет hivexget"}
    hive = hives[0]
    enr = run(["hivexregedit", "--export", hive, "\\Microsoft\\Enrollments"]) or ""
    ap = run(["hivexregedit", "--export", hive, "\\Microsoft\\Provisioning\\Diagnostics\\AutoPilot"]) or ""
    enrolled = sum(1 for ln in enr.splitlines() if ln.startswith("[") and "Enrollments\\" in ln and ln.count("\\") >= 4)
    return {"checked": True, "mdm_enrollment_keys": enrolled, "autopilot_keys": 1 if ap.strip() else 0, "reason": None}


def firmware_flags():
    """Признаки прошивки без сети: таблица MSDM (ключ Windows в плате), режим Secure Boot. Это сведения, а не вердикт."""
    return {
        "acpi_msdm": os.path.exists(f"{SYS}/firmware/acpi/tables/MSDM") if os.path.isdir(f"{SYS}/firmware/acpi/tables") else None,
        "efi": os.path.isdir(f"{SYS}/firmware/efi"),
    }


def collect(sticker, dev=None):
    return {
        "board_serial": dmi("baseboard-serial-number"),
        "chassis_serial": dmi("system-serial-number"),
        "sticker_serial": sticker,
        "model": dmi("system-product-name"),
        "bios_vendor": dmi("bios-vendor"),
        "bios_version": dmi("bios-version"),
        "battery": battery(),
        "ssd": ssd(dev),
        "flags": {**hive_flags(), **firmware_flags()},
    }


def send(url, token, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json", "X-Radar-Token": token}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser(description="Телеметрия ноутбука для приёмки (только чтение)")
    ap.add_argument("--sticker", required=True, help="серийный номер с наклейки на корпусе")
    ap.add_argument("--dev", help="накопитель для SMART, например /dev/nvme0n1")
    ap.add_argument("--url", help="адрес вебхука n8n")
    ap.add_argument("--token", default=os.environ.get("INTAKE_TOKEN"), help="токен приёмки (или переменная INTAKE_TOKEN)")
    ap.add_argument("--dry-run", action="store_true", help="только показать данные, ничего не отправлять")
    a = ap.parse_args()
    data = collect(a.sticker, a.dev)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if a.dry_run or not a.url:
        return 0
    if not a.token:
        print("нет токена: --token или INTAKE_TOKEN", file=sys.stderr)
        return 2
    status, body = send(a.url, a.token, data)
    print(f"ответ сервера {status}: {body}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
