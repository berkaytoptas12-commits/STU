from pathlib import Path

import pytest


@pytest.mark.parametrize("question, expected", [
    ("ARINC 429 kelimesinde SDI bitleri", "arinc"),
    ("AFDX BAG değerleri nelerdir?", "arinc"),
    ("DDR4 tRFC zamanlaması nedir?", "ddr"),
    ("LPDDR5 ZQ kalibrasyonu", "ddr"),
    ("PCIe Gen3 LTSSM Polling", "pcie"),
    ("PCI Express TLP başlık formatı", "pcie"),
    ("1000BASE-T Ethernet çerçeve boyutu", "ethernet"),
    ("RGMII zamanlama gereksinimleri", "ethernet"),
    ("DisplayPort HBR3 şerit hızı", "displayport"),
    ("eDP 1.4 AUX channel", "displayport"),
    ("USB 3.2 Gen 2 veri hızı", "usb"),
    ("Type-C CC pini direnci", "usb"),
    ("RS-422 maksimum kablo uzunluğu", "rs422"),
    ("TIA/EIA-422 sürücü çıkış gerilimi", "rs422"),
    ("I2C Fast-mode Plus hızı", "i2c"),
    ("SMBus timeout süresi", "i2c"),
    ("DO-254 DAL seviyeleri", "general"),
    ("MIL-STD-461 CE102 limiti", "general"),
])
def test_detect(registry, question, expected):
    assert registry.detect(question)[0] == expected


def test_no_false_domain_for_generic_question(registry):
    assert registry.detect("Diferansiyel çiftlerde empedans kontrolü nasıl yapılır?") == []


def test_turkish_term_expansion_handles_suffixes_and_softening(registry):
    terms = registry.expand_terms("Maksimum kablo uzunluğu ve sonlandırma direnci")
    assert {"maximum", "cable", "length", "termination"} <= set(" ".join(terms).split())
    assert "load" not in registry.expand_terms("yüksek hızlı sinyal")  # 'yük' must not hit 'yüksek'


def test_acronym_expansion(registry):
    terms = registry.expand_terms("LTSSM durumları")
    assert "LTSSM Link Training and Status State Machine" in terms


def test_classify_document(registry):
    assert registry.classify_document(Path("pcie/spec.pdf"), "") == "pcie"
    assert registry.classify_document(Path("PCI_Express_Base_5.0.pdf"), "") == "pcie"
    assert registry.classify_document(Path("unknown.pdf"), "USB VBUS USB Type-C USB") == "usb"
    assert registry.classify_document(Path("unknown.pdf"), "nothing relevant") == "general"
