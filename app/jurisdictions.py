"""Where the overseas entity of a lead can be registered, and which registries count as official evidence.

Only official company registers (or the government portal that hosts them) are listed: a record found
anywhere else - aggregators, directories, the company's own site - is never accepted as registration proof.
The U.S. is handled by the original rules in rules.py (LLC designator, state code, Secretary of State).
"""
import re

# code: (name, accepted company forms, official hosts, official host suffixes, where to look)
_J = {
    "US": ("United States", ("LLC",), (), (), "the state Secretary of State business registry"),
    "GB": ("United Kingdom", ("Ltd", "Limited", "LLP", "PLC"), (), (".gov.uk",),
           "Companies House (find-and-update.company-information.service.gov.uk)"),
    "AU": ("Australia", ("Pty Ltd", "Pty. Ltd.", "Pty Limited", "Proprietary Limited", "Ltd", "Limited"), (),
           (".gov.au",), "ABN Lookup (abr.business.gov.au) or ASIC Connect (connectonline.asic.gov.au)"),
    "NZ": ("New Zealand", ("Ltd", "Limited"), (), (".govt.nz",), "NZ Companies Office (app.companiesoffice.govt.nz)"),
    # European Union
    "AT": ("Austria", ("GmbH", "AG"), (), (".gv.at",), "the Firmenbuch (justiz.gv.at)"),
    "BE": ("Belgium", ("BV", "SRL", "NV", "SA", "BVBA", "SPRL"), (), (".fgov.be",),
           "the Crossroads Bank for Enterprises (kbopub.economie.fgov.be)"),
    "BG": ("Bulgaria", ("EOOD", "OOD", "AD", "ЕООД", "ООД"), ("registryagency.bg", "brra.bg"), (),
           "the Commercial Register (portal.registryagency.bg)"),
    "HR": ("Croatia", ("d.o.o.", "j.d.o.o.", "d.d."), ("pravosudje.hr",), (), "the Court Register (sudreg.pravosudje.hr)"),
    "CY": ("Cyprus", ("Ltd", "Limited"), (), (".gov.cy",), "the Registrar of Companies (efiling.drcor.mcit.gov.cy)"),
    "CZ": ("Czechia", ("s.r.o.", "a.s."), ("justice.cz",), (), "the Public Register (or.justice.cz)"),
    "DK": ("Denmark", ("ApS", "A/S", "IVS"), ("virk.dk", "cvr.dk"), (), "the CVR register (datacvr.virk.dk)"),
    "EE": ("Estonia", ("OÜ", "OU", "AS"), ("rik.ee",), (), "the e-Business Register (ariregister.rik.ee)"),
    "FI": ("Finland", ("Oy", "Oyj", "Ab"), ("ytj.fi", "prh.fi"), (), "the Business Information System (ytj.fi)"),
    "FR": ("France", ("SAS", "SASU", "SARL", "EURL", "SA"), ("infogreffe.fr", "inpi.fr"), (".gouv.fr",),
           "annuaire-entreprises.data.gouv.fr or Infogreffe"),
    "DE": ("Germany", ("GmbH", "UG (haftungsbeschränkt)", "UG", "AG"), ("handelsregister.de", "unternehmensregister.de"),
           (), "the Handelsregister (handelsregister.de)"),
    "GR": ("Greece", ("IKE", "I.K.E.", "EPE", "E.P.E.", "AE", "A.E.", "S.A."), ("businessportal.gr",), (".gov.gr",),
           "the General Commercial Registry (businessportal.gr)"),
    "HU": ("Hungary", ("Kft.", "Kft", "Zrt.", "Zrt", "Nyrt."), ("e-cegjegyzek.hu",), (), "the company register (e-cegjegyzek.hu)"),
    "IE": ("Ireland", ("Ltd", "Limited", "DAC", "CLG", "PLC", "Teoranta", "Teo"), ("cro.ie",), (".gov.ie",),
           "the Companies Registration Office (core.cro.ie)"),
    "IT": ("Italy", ("S.r.l.", "Srl", "S.r.l.s.", "S.p.A.", "SpA"), ("registroimprese.it",), (),
           "the Registro Imprese (registroimprese.it)"),
    "LV": ("Latvia", ("SIA", "AS"), (), (".gov.lv",), "the Register of Enterprises (ur.gov.lv)"),
    "LT": ("Lithuania", ("UAB", "AB", "MB"), ("registrucentras.lt",), (), "the Register of Legal Entities (registrucentras.lt)"),
    "LU": ("Luxembourg", ("S.à r.l.", "SARL", "S.A.", "SA"), ("lbr.lu",), (), "the Luxembourg Business Registers (lbr.lu)"),
    "MT": ("Malta", ("Ltd", "Limited", "p.l.c."), ("mbr.mt",), (), "the Malta Business Registry (registry.mbr.mt)"),
    "NL": ("Netherlands", ("B.V.", "BV", "N.V.", "NV"), ("kvk.nl",), (), "the KVK Business Register (kvk.nl)"),
    "PL": ("Poland", ("Sp. z o.o.", "S.A."), (), (".gov.pl",), "the National Court Register (wyszukiwarka-krs.ms.gov.pl)"),
    "PT": ("Portugal", ("Lda", "Lda.", "Unipessoal Lda", "S.A."), ("mj.pt",), (".gov.pt",),
           "the commercial register (publicacoes.mj.pt)"),
    "RO": ("Romania", ("S.R.L.", "SRL", "S.A."), ("onrc.ro",), (), "the Trade Register (onrc.ro)"),
    "SK": ("Slovakia", ("s.r.o.", "a.s."), ("orsr.sk",), (), "the Business Register (orsr.sk)"),
    "SI": ("Slovenia", ("d.o.o.", "d.d."), ("ajpes.si",), (), "the Business Register (ajpes.si)"),
    "ES": ("Spain", ("S.L.", "SL", "S.L.U.", "SLU", "S.A.", "SA"), ("registradores.org", "rmc.es", "boe.es"), (),
           "the Registro Mercantil (registradores.org / BORME on boe.es)"),
    "SE": ("Sweden", ("AB", "Aktiebolag"), ("bolagsverket.se",), (), "Bolagsverket (bolagsverket.se)"),
    # EEA (outside the EU)
    "IS": ("Iceland", ("ehf.", "ehf", "hf.", "hf"), ("skatturinn.is", "rsk.is"), (), "the Register of Enterprises (skatturinn.is)"),
    "LI": ("Liechtenstein", ("AG", "GmbH", "Anstalt"), ("oera.li",), (), "the Commercial Register (oera.li)"),
    "NO": ("Norway", ("AS", "ASA"), ("brreg.no",), (), "the Brønnøysund Register (brreg.no)"),
}

EU = ("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
      "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE")
EEA_EXTRA = ("IS", "LI", "NO")
MAIN = ("US", "GB", "AU", "NZ")
GROUPS = {"EU": EU, "EEA": EU + EEA_EXTRA}

ALIASES = {"UK": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB", "SCOTLAND": "GB",
           "WALES": "GB", "NORTHERN IRELAND": "GB", "USA": "US", "U.S.": "US", "U.S.A.": "US",
           "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "EL": "GR", "CZECH REPUBLIC": "CZ"}
ALIASES.update({v[0].upper(): k for k, v in _J.items()})

# Common Indian cities and states for the research focus picker (free text is also accepted).
INDIAN_CITIES = ("Delhi NCR", "Mumbai", "Bengaluru", "Hyderabad", "Chennai", "Pune", "Kolkata", "Ahmedabad",
                 "Noida", "Gurugram", "Jaipur", "Kochi", "Chandigarh", "Mohali", "Indore", "Coimbatore", "Surat",
                 "Lucknow", "Bhubaneswar", "Thiruvananthapuram", "Nagpur", "Vadodara")
INDIAN_STATES = ("Andhra Pradesh", "Delhi", "Goa", "Gujarat", "Haryana", "Karnataka", "Kerala", "Madhya Pradesh",
                 "Maharashtra", "Odisha", "Punjab", "Rajasthan", "Tamil Nadu", "Telangana", "Uttar Pradesh",
                 "West Bengal")


def name(code):
    j = _J.get(code)
    return j[0] if j else code


def forms(code):
    j = _J.get(code)
    return j[1] if j else ()


def registry(code):
    j = _J.get(code)
    return j[4] if j else ""


def norm_country(value):
    """'uk', 'United Kingdom', 'GB' -> 'GB'. Unknown input -> ''."""
    v = " ".join(str(value or "").upper().split())
    if v in _J:
        return v
    return ALIASES.get(v, "")


def expand_markets(values):
    """Market selection (codes plus the 'EU' / 'EEA' shortcuts) -> ordered unique country codes."""
    out = []
    for v in values or ():
        v = str(v).strip().upper()
        for code in GROUPS.get(v, (norm_country(v),)):
            if code and code not in out:
                out.append(code)
    return out


_FORM_RE = {code: re.compile(r"(?:^|[\s,])(?:" + "|".join(re.escape(f) for f in sorted(j[1], key=len, reverse=True))
                             + r")\.?\s*$", re.I) for code, j in _J.items() if code != "US"}


def has_form(code, entity_name):
    rx = _FORM_RE.get(code)
    return bool(rx and entity_name and rx.search(str(entity_name).strip()))


def is_official(code, host):
    """True if host (already lower-cased, without www.) belongs to that country's official company register."""
    j = _J.get(code)
    if not j or not host or code == "US":
        return False
    hosts, suffixes = j[2], j[3]
    return any(host == h or host.endswith("." + h) for h in hosts) or bool(suffixes and host.endswith(suffixes))
