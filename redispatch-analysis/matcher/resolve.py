"""The per-plant resolver: routes one redispatch name through the stage
cascade and returns a single record with per-stage detail plus the final
coordinate, source and confidence.
"""

import time

import pandas as pd
import requests

from config import (DE_BBOX, USE_WIKI, USE_DDG, USE_NOMINATIM_LAST_RESORT,
                    NOM_SLEEP, HEADERS)
from cache import CACHE
from classify import (classify_aggregation, GRID_AREA_NONGEO_RE,
                      FOREIGN_MARKERS)
from confidence import _confidence
from match_geo import (DIRECTION_TOKENS, match_gazetteer, match_state,
                       match_region, match_grid_area, nominatim_forward,
                       state_centroid, region_centroid)
from match_registry import match_opsd, match_opsd_city, match_psa
from match_web import DDGS, in_de_bbox, search_wikipedia, search_ddg_wiki
from normalize import clean_redispatch_name, norm, norm_tokens


def resolve(name, primaer, opsd, psa):
    primaer_l = (primaer or "").lower() or None
    agg = classify_aggregation(name)
    cleaned, tokens, cleaned_raw = clean_redispatch_name(name)
    cleaned_key = norm(cleaned)

    rec = dict(
        # ---- debug-first columns (leftmost in output CSV) ----
        plant_name=name,
        final_confidence=0.0,
        final_source=None,
        final_lat=None, final_lon=None,
        primaerenergieart=primaer,
        cleaned_name=cleaned,
        place_tokens=" ".join(tokens),
        aggregation=agg,
        # ---- per-stage detail ----
        opsd_match="none", opsd_score=None, opsd_energy_match=None,
        opsd_matched_name=None, opsd_lat=None, opsd_lon=None, opsd_class=None,
        psa_match="none", psa_score=None, psa_energy_match=None,
        psa_matched_name=None, psa_lat=None, psa_lon=None, psa_class=None,
        psa_capacity=None, psa_n_candidates=0,
        wiki_match="none", wiki_title=None, wiki_lat=None, wiki_lon=None,
        wiki_url=None, wiki_reason=None,
        ddg_match="none",  ddg_title=None,  ddg_lat=None,  ddg_lon=None,
        ddg_url=None,  ddg_reason=None,
        gaz_match="none", gaz_place=None, gaz_state=None,
        gaz_lat=None, gaz_lon=None,
        state_match="none", state_name=None, state_direction=None,
        state_lat=None, state_lon=None,
    )

    # ----- Stage 0: aggregation / foreign filters -----
    if agg == "grid_area":
        # Pure market constructs (Börse, MRL, 10Y EICs, abschaltbare Last,
        # Netzregelverbund, GESAMTEINSPEISUNG) have no meaningful coordinate.
        if GRID_AREA_NONGEO_RE.search(name):
            rec["final_source"]     = "skipped_grid_area_market"
            rec["final_confidence"] = 0.0
            return rec
        # Otherwise try to extract a region / state / DSO area.
        gres = match_grid_area(name)
        if gres is not None:
            src, conf, lat, lon, label = gres
            rec["state_match"] = "grid_area"
            rec["state_name"]  = label
            rec["state_lat"]   = lat
            rec["state_lon"]   = lon
            rec["final_source"]     = src
            rec["final_confidence"] = conf
            rec["final_lat"], rec["final_lon"] = lat, lon
            return rec
        rec["final_source"]     = "skipped_grid_area_unresolvable"
        rec["final_confidence"] = 0.0
        return rec
    # Foreign skip is now a no-op (FOREIGN_MARKERS never matches), kept for
    # forward compatibility.
    if FOREIGN_MARKERS.search(name):
        rec["final_source"] = "skipped_foreign"
        return rec

    HIGH_CONF_SHORTCIRCUIT = 0.85   # any single source >= this -> we stop early
    candidates = []                  # list of (source, confidence, lat, lon)

    def add_cand(source, confidence, lat, lon):
        if (confidence is not None and confidence > 0
                and lat is not None and lon is not None):
            candidates.append((source, float(confidence), float(lat), float(lon)))

    def best_so_far():
        return max((c[1] for c in candidates), default=0.0)
###this here is highly adapted to the dataset and not really all that clean but it works so i left it in :)
    # ----- Stage 0b: state-aggregate intercept for renewables -----
    if primaer_l == "erneuerbar" and tokens and len(tokens) <= 2:
        sres_early = match_state(tokens)
        if sres_early is not None:
            rec["state_match"]     = "match"
            rec["state_name"]      = sres_early["state"]
            rec["state_direction"] = sres_early.get("direction")
            rec["state_lat"]       = sres_early["lat"]
            rec["state_lon"]       = sres_early["lon"]
            src = ("state_centroid" if sres_early.get("direction") is None
                   else f"state_centroid_{sres_early['direction']}")
            conf = 0.35 if sres_early.get("direction") is None else 0.30
            add_cand(src, conf, sres_early["lat"], sres_early["lon"])
            # State aggregates are unlikely to have a better source, so we
            # short-circuit here even though confidence is low.
            return _finalize(rec, candidates)

    # ----- Stage 1: OPSD direct -----
    o_status, o_score, o_idx, o_eok = match_opsd(cleaned_key, primaer_l, opsd)
    rec["opsd_match"] = o_status
    rec["opsd_score"] = o_score if o_status != "none" else None
    rec["opsd_energy_match"] = (True if o_eok is True
                                else False if o_eok is False else None)
    if o_idx is not None:
        rec["opsd_matched_name"] = opsd.at[o_idx, "name_bnetza"]
        rec["opsd_lat"] = float(opsd.at[o_idx, "lat"])
        rec["opsd_lon"] = float(opsd.at[o_idx, "lon"])
        rec["opsd_class"] = opsd.at[o_idx, "class"]
        if rec["opsd_energy_match"] is not False:
            conf = _confidence("opsd", o_status, o_score,
                               rec["opsd_energy_match"], 1)
            add_cand(f"opsd_{o_status}", conf, rec["opsd_lat"], rec["opsd_lon"])

    # ----- Stage 2: PSA direct -----
    p_status, p_score, p_idx, p_eok, p_ncands = match_psa(
        cleaned_key, primaer_l, psa)
    rec["psa_match"] = p_status
    rec["psa_score"] = p_score if p_status != "none" else None
    rec["psa_energy_match"] = (True if p_eok is True
                               else False if p_eok is False else None)
    rec["psa_n_candidates"] = p_ncands
    if p_idx is not None:
        rec["psa_matched_name"] = psa.at[p_idx, "Name"]
        rec["psa_lat"] = float(psa.at[p_idx, "lat"])
        rec["psa_lon"] = float(psa.at[p_idx, "lon"])
        rec["psa_class"] = psa.at[p_idx, "class"]
        rec["psa_capacity"] = (float(psa.at[p_idx, "Capacity"])
                               if pd.notna(psa.at[p_idx, "Capacity"]) else None)
        if rec["psa_energy_match"] is not False:
            conf = _confidence("psa", p_status, p_score,
                               rec["psa_energy_match"], p_ncands)
            add_cand(f"psa_{p_status}", conf, rec["psa_lat"], rec["psa_lon"])

    # Short-circuit ONLY on high confidence. A weak PSA fuzzy doesn't get to
    # block Wikipedia from running -- it stays in the candidate pool and may
    # still win at the end if nothing else fires.
    if best_so_far() >= HIGH_CONF_SHORTCIRCUIT:
        return _finalize(rec, candidates)

    # ----- Stage 2b: directional-retry on OPSD/PSA -----
    # 'Baltic 1+2 Nord' / 'Baltic 1+2 Sued' should reuse the base 'Baltic'
    # match plus a directional offset, not fail because the full key has no
    # exact PSA hit.
    if (len(tokens) >= 2 and tokens[-1] in DIRECTION_TOKENS
            and best_so_far() < HIGH_CONF_SHORTCIRCUIT):
        alt_tokens = tokens[:-1]
        alt_key    = norm(" ".join(alt_tokens))
        if alt_key and alt_key != cleaned_key:
            o2_status, o2_score, o2_idx, o2_eok = match_opsd(
                alt_key, primaer_l, opsd)
            if o2_status != "none" and o2_eok is not False:
                conf = _confidence("opsd", o2_status, o2_score, o2_eok, 1)
                add_cand(f"opsd_{o2_status}_dir",
                         conf * 0.9,   # small penalty for stripped direction
                         float(opsd.at[o2_idx, "lat"]),
                         float(opsd.at[o2_idx, "lon"]))
            p2_status, p2_score, p2_idx, p2_eok, p2_nc = match_psa(
                alt_key, primaer_l, psa)
            if p2_status != "none" and p2_eok is not False:
                conf = _confidence("psa", p2_status, p2_score, p2_eok, p2_nc)
                add_cand(f"psa_{p2_status}_dir",
                         conf * 0.9,
                         float(psa.at[p2_idx, "lat"]),
                         float(psa.at[p2_idx, "lon"]))
            if best_so_far() >= HIGH_CONF_SHORTCIRCUIT:
                return _finalize(rec, candidates)

    # ----- Stage 3: wiki/DDG for konventionell + sonstiges -----
    if primaer_l in ("konventionell", "sonstiges"):
        # Try several plant-type suffixes in turn — more specific first so a
        # 'Kohlekraftwerk Boxberg' query gets a sharper Wikipedia ranking than
        # the bare 'Kraftwerk' search. The generic 'Kraftwerk' stays in the
        # list as a catch-all.
        nl = name.lower()
        if primaer_l == "konventionell":
            suffixes_k = []
            if any(s in nl for s in ("kohle", "stk", "braunkohle", "stein")):
                suffixes_k.append("Kohlekraftwerk")
            if any(s in nl for s in ("gas", "gud", "gd", "ccgt", "ocgt", "gt")):
                suffixes_k.append("Gaskraftwerk")
            if any(s in nl for s in ("kkw", "akw", "kern", "nuclear")):
                suffixes_k.append("Kernkraftwerk")
            if any(s in nl for s in ("hkw", "bhkw", "heiz", "kwk")):
                suffixes_k.append("Heizkraftwerk")
            suffixes_k.append("Kraftwerk")
        else:  # sonstiges (mostly hydro / pumped storage)
            suffixes_k = ["Pumpspeicherkraftwerk", "Wasserkraftwerk",
                          "Speicherkraftwerk", "Kraftwerk"]

        wres_last = None
        if USE_WIKI:
            for suffix in suffixes_k:
                cache_key = f"wiki|{suffix}|{cleaned_key}|{primaer_l}"
                if cache_key in CACHE:
                    wres = CACHE[cache_key]
                else:
                    wres = search_wikipedia(tokens, primaer_l, suffix,
                                            raw_query=cleaned_raw)
                    CACHE[cache_key] = wres
                wres_last = wres
                rec["wiki_match"]  = wres["status"]
                rec["wiki_title"]  = wres.get("title")
                rec["wiki_lat"]    = wres.get("lat")
                rec["wiki_lon"]    = wres.get("lon")
                rec["wiki_url"]    = wres.get("url")
                rec["wiki_reason"] = f"{suffix}:{wres.get('reason')}"
                if wres["status"] == "match":
                    add_cand("wikipedia",
                             _confidence("wiki", "match", 1.0, True, 1),
                             wres["lat"], wres["lon"])
                    break

        if (USE_DDG and DDGS is not None and rec["wiki_match"] != "match"
                and wres_last is not None and wres_last["status"] == "rejected"):
            for suffix in suffixes_k:
                dkey = f"ddg|{suffix}|{cleaned_key}|{primaer_l}"
                if dkey in CACHE:
                    dres = CACHE[dkey]
                else:
                    dres = search_ddg_wiki(tokens, primaer_l, suffix,
                                           raw_query=cleaned_raw)
                    CACHE[dkey] = dres
                rec["ddg_match"]  = dres["status"]
                rec["ddg_title"]  = dres.get("title")
                rec["ddg_lat"]    = dres.get("lat")
                rec["ddg_lon"]    = dres.get("lon")
                rec["ddg_url"]    = dres.get("url")
                rec["ddg_reason"] = f"{suffix}:{dres.get('reason')}"
                if dres["status"] == "match":
                    add_cand("duckduckgo",
                             _confidence("ddg", "match", 1.0, True, 1),
                             dres["lat"], dres["lon"])
                    break

    if best_so_far() >= HIGH_CONF_SHORTCIRCUIT:
        return _finalize(rec, candidates)

    # ----- Stage 3b: OPSD city-only -----
    c_status, c_score, c_idx, c_eok = match_opsd_city(cleaned_key,
                                                      primaer_l, opsd)
    if c_status == "city":
        # Only overwrite OPSD fields if they're still empty.
        if rec["opsd_match"] == "none":
            rec["opsd_match"] = c_status
            rec["opsd_score"] = c_score
            rec["opsd_energy_match"] = (True if c_eok is True
                                        else False if c_eok is False else None)
            rec["opsd_matched_name"] = opsd.at[c_idx, "name_bnetza"]
            rec["opsd_lat"] = float(opsd.at[c_idx, "lat"])
            rec["opsd_lon"] = float(opsd.at[c_idx, "lon"])
            rec["opsd_class"] = opsd.at[c_idx, "class"]
        add_cand("opsd_city",
                 _confidence("opsd", "city", c_score,
                             rec["opsd_energy_match"], 1),
                 float(opsd.at[c_idx, "lat"]), float(opsd.at[c_idx, "lon"]))

    # ----- Stage 4a: state-centroid -----
    sres = match_state(tokens)
    if sres is not None and rec["state_match"] == "none":
        rec["state_match"]     = "match"
        rec["state_name"]      = sres["state"]
        rec["state_direction"] = sres.get("direction")
        rec["state_lat"]       = sres["lat"]
        rec["state_lon"]       = sres["lon"]
        src = ("state_centroid" if sres.get("direction") is None
               else f"state_centroid_{sres['direction']}")
        conf = 0.35 if sres.get("direction") is None else 0.30
        add_cand(src, conf, sres["lat"], sres["lon"])

    # ----- Stage 4b: gazetteer -----
    gres = match_gazetteer(tokens)
    rec["gaz_match"] = gres["status"]
    rec["gaz_place"] = gres.get("place")
    rec["gaz_state"] = gres.get("state")
    rec["gaz_lat"]   = gres.get("lat")
    rec["gaz_lon"]   = gres.get("lon")
    if gres["status"] == "match":
        add_cand("gazetteer",
                 _confidence("gaz", "match", 1.0, True, 1),
                 gres["lat"], gres["lon"])

    # ----- Stage 4c: region centroid (Emsland, Sauerland, ...) -----
    # Plant names that contain a Region name but aren't classified as
    # grid_area (e.g. 'EMSLAND_DTB0' is konventionell — the Kernkraftwerk
    # Emsland's gas turbines). Region match is low confidence but better
    # than 'unresolved'.
    rres = match_region(tokens)
    if rres is not None:
        src = ("grid_area_region" if rres.get("direction") is None
               else f"grid_area_region_{rres['direction']}")
        conf = 0.30 if rres.get("direction") is None else 0.25
        add_cand(src, conf, rres["lat"], rres["lon"])

    # ----- Stage 5: wiki/DDG for renewables (if not yet a wiki match) -----
    if (primaer_l == "erneuerbar"
            and rec["wiki_match"] != "match"):
        nl = name.lower()
        is_offshore = any(s in nl for s in ("owp", "offshore"))
        if any(s in nl for s in ("owp", "windpark", "windkraft",
                                  " wp ", "_wp_", "wind")):
            # Offshore-Windpark first for OWP/Offshore-tagged names — it
            # disambiguates 'Wikinger' (the SWT-5.0 wind farm north of Rügen)
            # from 'Wikinger Museum Haithabu' in Schleswig.
            suffixes = (["Offshore-Windpark", "Windpark", "Windkraftanlage"]
                        if is_offshore
                        else ["Windpark", "Windkraftanlage"])
        elif any(s in nl for s in ("solarpark", "pv", "photovolt", "solar")):
            suffixes = ["Solarpark", "Photovoltaikanlage",
                        "PV-Freiflächenanlage"]
        else:
            suffixes = ["Windpark", "Solarpark"]

        wres_last = None
        if USE_WIKI:
            for suffix in suffixes:
                wkey = f"wiki|{suffix}|{cleaned_key}|{primaer_l}"
                if wkey in CACHE:
                    wres = CACHE[wkey]
                else:
                    wres = search_wikipedia(tokens, primaer_l, suffix,
                                            raw_query=cleaned_raw)
                    CACHE[wkey] = wres
                wres_last = wres
                rec["wiki_match"]  = wres["status"]
                rec["wiki_title"]  = wres.get("title")
                rec["wiki_lat"]    = wres.get("lat")
                rec["wiki_lon"]    = wres.get("lon")
                rec["wiki_url"]    = wres.get("url")
                rec["wiki_reason"] = f"{suffix}:{wres.get('reason')}"
                if wres["status"] == "match":
                    add_cand("wikipedia",
                             _confidence("wiki", "match", 1.0, True, 1),
                             wres["lat"], wres["lon"])
                    break

        if (USE_DDG and DDGS is not None and rec["wiki_match"] != "match"
                and wres_last is not None and wres_last["status"] == "rejected"):
            for suffix in suffixes:
                dkey = f"ddg|{suffix}|{cleaned_key}|{primaer_l}"
                if dkey in CACHE:
                    dres = CACHE[dkey]
                else:
                    dres = search_ddg_wiki(tokens, primaer_l, suffix,
                                           raw_query=cleaned_raw)
                    CACHE[dkey] = dres
                rec["ddg_match"]  = dres["status"]
                rec["ddg_title"]  = dres.get("title")
                rec["ddg_lat"]    = dres.get("lat")
                rec["ddg_lon"]    = dres.get("lon")
                rec["ddg_url"]    = dres.get("url")
                rec["ddg_reason"] = f"{suffix}:{dres.get('reason')}"
                if dres["status"] == "match":
                    add_cand("duckduckgo",
                             _confidence("ddg", "match", 1.0, True, 1),
                             dres["lat"], dres["lon"])
                    break

    # ----- Stage 6: Nominatim last resort -----
    # When literally nothing matched, try a single Nominatim forward geocode
    # on the cleaned token list. Catches small Ortsteile (Pleinting,
    # Görries-like cases) that aren't in opensearch results or pgeocode.
    # Cached so we only pay the HTTP cost once per name.
    if not candidates and tokens and USE_NOMINATIM_LAST_RESORT:
        nkey = f"nom|{cleaned_key}"
        if nkey in CACHE:
            nres = CACHE[nkey]
        else:
            # Try the full token string first; if no hit, the first token.
            nres = (nominatim_forward(cleaned_raw or " ".join(tokens))
                    or (nominatim_forward(tokens[0])
                        if len(tokens) > 1 else None))
            time.sleep(NOM_SLEEP)
            CACHE[nkey] = nres
        if nres:
            lat, lon, state = nres
            if in_de_bbox(lat, lon):
                rec["gaz_match"] = "nominatim"
                rec["gaz_place"] = (cleaned_raw or " ".join(tokens)).title()
                rec["gaz_state"] = state
                rec["gaz_lat"]   = lat
                rec["gaz_lon"]   = lon
                add_cand("nominatim", 0.40, lat, lon)

    return _finalize(rec, candidates)


def _finalize(rec, candidates):
    """Pick the highest-confidence candidate from the pool and write the
    final_* fields. Returns the record."""
    if not candidates:
        rec["final_source"] = rec.get("final_source") or "unresolved"
        rec["final_confidence"] = 0.0
        return rec
    candidates.sort(key=lambda c: c[1], reverse=True)
    src, conf, lat, lon = candidates[0]
    rec["final_source"]     = src
    rec["final_confidence"] = round(conf, 3)
    rec["final_lat"]        = lat
    rec["final_lon"]        = lon
    return rec
