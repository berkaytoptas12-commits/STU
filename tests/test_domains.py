from pathlib import Path

import pytest


@pytest.mark.parametrize("question, collection, entity", [
    ("ARINC 429 kelimesinde SDI bitleri", "arinc", "ARINC 429"),
    ("AFDX BAG değerleri nelerdir?", "arinc", "ARINC 664"),
    ("DDR4 tRFC zamanlaması nedir?", "ddr", "DDR4"),
    ("LPDDR5 ZQ kalibrasyonu", "ddr", "LPDDR5"),
    ("PCIe Gen4 lane rate", "pcie", "PCIe 4.0"),
    ("PCI Express 5.0 equalization", "pcie", "PCIe 5.0"),
    ("1000BASE-T Ethernet IEEE 802.3 çerçeve boyutu", "ethernet", "IEEE 802.3"),
    ("RGMII zamanlama gereksinimleri", "ethernet", "RGMII"),
    ("DisplayPort 1.4 HBR3 şerit hızı", "displayport", "DisplayPort 1.4"),
    ("USB 3.2 Gen 2 veri hızı", "usb", "USB 3.x"),
    ("USB Power Delivery EPR gerilimi", "usb", "USB PD"),
    ("RS-422 maksimum kablo uzunluğu", "rs422", "TIA-422"),
    ("I2C Fast-mode Plus hızı", "i2c", "I2C"),
    ("SMBus timeout süresi", "i2c", "SMBus"),
    ("DO-254 DAL seviyeleri", "general", "DO-254"),
    ("MIL-STD-461 CE102 limiti", "general", "MIL-STD-461"),
])
def test_detect_collection_and_entity(registry, question, collection, entity):
    assert registry.detect(question)[0] == collection
    assert registry.detect_entities(question)[0] == entity


def test_no_cross_matches(registry):
    assert registry.detect_entities("LPDDR4X ZQ") == ["LPDDR4"], "ddr4 must not match inside lpddr4"
    assert "PCIe 3.x" not in registry.detect_entities("USB 3.2 Gen 2"), "'Gen 2' alone is not PCIe"
    assert registry.detect("Diferansiyel çiftlerde empedans kontrolü nasıl yapılır?") == []


def test_document_entities_prefer_filename_and_dominant_front_matter(registry):
    assert registry.document_entities("ddr", "JESD79-5B_DDR5", "", "unlike DDR4 ...") == ["DDR5"]
    assert registry.document_entities("pcie", "spec", "PCI Express Base Specification Revision 5.0", "") == ["PCIe 5.0"]
    front = "DDR5 SDRAM. DDR5 devices ... DDR5 refresh ... compared with DDR4."
    assert registry.document_entities("ddr", "spec", "", front) == ["DDR5"]


def test_unknown_folder_becomes_collection(registry):
    assert registry.classify_document(Path("spacewire/ECSS-E-ST-50-12C.pdf"), "") == "spacewire"
    assert "spacewire" in registry.keys()


def test_turkish_term_expansion(registry):
    terms = " ".join(registry.expand_terms("Maksimum kablo uzunluğu ve sonlandırma direnci"))
    for w in ("maximum", "cable", "length", "termination"):
        assert w in terms
    assert "load" not in registry.expand_terms("yüksek hızlı sinyal")
