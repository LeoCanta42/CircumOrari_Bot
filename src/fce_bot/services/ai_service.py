"""AI Assistant Service for natural language transit Q&A, fuzzy stop matching, nearest-stop geocoding, and path tracing."""

import json
import logging
import re
from datetime import datetime
from typing import Any, List, Optional

import requests

from fce_bot.config import settings
from fce_bot.core.models import AIQueryResponse, FilterType, TransportType
from fce_bot.core.time_utils import (
    TIME_DELIMITERS_PATTERN,
    get_current_time_str,
    get_local_now,
    resolve_query_time,
)
from fce_bot.data.city_resolver import geocode_place_in_etna_area
from fce_bot.data.db import get_db, normalize_stop_name
from fce_bot.services.documents_service import documents_service
from fce_bot.services.transit_service import LocationResolution, transit_service

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Sei l'assistente virtuale per la mobilità CircumOrari (Ferrovia Circumetnea e Metropolitana di Catania).
Il tuo compito è aiutare i viaggiatori a pianificare i loro spostamenti, indicando gli orari corretti, le fermate di partenza e arrivo (anche quando il passeggero scrive un nome approssimativo, un indirizzo o un luogo di interesse), il percorso delle linee e le informazioni su biglietti e documenti.
REGOLE FONDAMENTALI DI RISPOSTA:
1. Rispondi sempre in lingua italiana, in modo cordiale, chiaro e naturale.
3. Se la fermata indicata dall'utente aveva un nome simile ma non identico (es. "Catania Santa Sofia" -> "Parcheggio Santa Sofia"), segnala cortesemente quale fermata reale hai utilizzato.
4. Se l'utente ha indicato un indirizzo o un luogo generico (es. Piazza Duomo, Via Etnea, Policlinico), indica chiaramente la fermata più vicina e la relativa distanza indicativa a piedi, spiegando il percorso trovato.
5. Quando utile o richiesto, indica anche le fermate intermedie attraversate dalla linea lungo il tragitto.
6. Se l'utente chiede informazioni su orari completi o documenti, dì semplicemente: "Puoi consultare e scaricare gli orari aggiornati ufficiali nella sezione dedicata o sul sito circumetnea.it".
7. Per i biglietti, indica che si possono acquistare nelle stazioni, alle emettitrici automatiche della metropolitana, nelle rivendite autorizzate (tabaccherie/edicole) e online tramite app o DropTicket.
8. Evita sintassi markdown complessa o asterischi multipli sparsi. Basa orari e percorsi esclusivamente sui dati ufficiali forniti nel contesto.
"""


class ExtractedEntities(tuple):
    """Backwards-compatible tuple representing extracted query components.

    Can be unpacked as 4 items: (origin, destination, time, transport_type)
    or accessed with properties .origin, .destination, .time, .transport_type, .filter_type,
    .line_to_trace, .place_for_nearest_stop, .has_explicit_time.
    """

    def __new__(
        cls,
        origin,
        destination,
        time_val,
        ttype,
        ftype=FilterType.DEPARTURE_AFTER,
        line_to_trace: Optional[str] = None,
        place_for_nearest_stop: Optional[str] = None,
        has_explicit_time: bool = False,
    ):
        return super().__new__(cls, (origin, destination, time_val, ttype))

    def __init__(
        self,
        origin,
        destination,
        time_val,
        ttype,
        ftype=FilterType.DEPARTURE_AFTER,
        line_to_trace: Optional[str] = None,
        place_for_nearest_stop: Optional[str] = None,
        has_explicit_time: bool = False,
    ):
        self.origin = origin
        self.destination = destination
        self.time = time_val
        self.transport_type = ttype
        self.filter_type = ftype
        self.line_to_trace = line_to_trace
        self.place_for_nearest_stop = place_for_nearest_stop
        self.has_explicit_time = has_explicit_time


class AIService:
    """Natural Language processing and LLM-assisted trip advisor."""

    def __init__(self):
        self._all_stop_names_cache = None

    def _get_known_stops(self) -> list:
        if self._all_stop_names_cache is None:
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    rows = cur.execute("SELECT DISTINCT stop_name, normalized_name FROM stops").fetchall()
                    self._all_stop_names_cache = [(r["stop_name"], r["normalized_name"]) for r in rows]
            except Exception:
                self._all_stop_names_cache = []
        return self._all_stop_names_cache

    def _clean_entity(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        c = name.strip()
        c = re.sub(
            r"^(?:(?:ad\s+)?(?:essere|arrivare|andare)\s+(?:a|ad|in)\s+|la\s+stazione\s+di|stazione\s+di|la\s+fermata\s+di|fermata\s+di|stazione|fermata|il\s+comune\s+di|comune\s+di|la\s+città\s+di|vicino\s+a|nei\s+pressi\s+di)\s+",
            "",
            c,
            flags=re.I,
        )
        c = re.sub(
            rf"\s+{TIME_DELIMITERS_PATTERN}(?:\s+.*)?$",
            "",
            c,
            flags=re.I,
        )
        c = re.sub(
            r"\s+(?:per\s+le|entro\s+le|alle|verso\s+le|verso|ore|domani|oggi|stasera|stamattina|dopo|prima)(?:\s+.*)?$",
            "",
            c,
            flags=re.I,
        )
        c = re.sub(r"\s+(?:in\s+treno|in\s+metro|in\s+metropolitana|in\s+bus|con\s+il\s+treno|con\s+la\s+metro|con\s+il\s+bus)$", "", c, flags=re.I)
        c = re.sub(r"[\?\.!,;:]+$", "", c)
        return c.strip() or None

    def _map_query_with_llm(self, text: str, now: Optional[datetime] = None) -> dict:
        """Generalized semantic mapper: calls Gemini to extract origin, destination, addresses, line tracing, and intent."""
        if not settings.GEMINI_API_KEY:
            return {}
        try:
            api_key = settings.GEMINI_API_KEY
            model = settings.GEMINI_MODEL or "gemini-3.5-flash-lite"
            ref_now = now or get_local_now()
            cur_time_str = ref_now.strftime("%H:%M")
            cur_date_str = ref_now.strftime("%Y-%m-%d")

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            prompt = (
                f"Sei l'analizzatore semantico della Ferrovia Circumetnea e Metropolitana di Catania.\n"
                f"Data e ora corrente locale: {cur_date_str} {cur_time_str} (fuso orario Europe/Rome).\n"
                f"Analizza la richiesta dell'utente: \"{text}\"\n\n"
                "Estrai le informazioni in formato JSON con questa struttura esatta:\n"
                "{\n"
                "  \"origin\": \"stazione, comune, via/indirizzo o luogo di partenza (oppure null)\",\n"
                "  \"destination\": \"stazione, comune, via/indirizzo o luogo di arrivo (oppure null)\",\n"
                f"  \"time\": \"orario in formato HH:MM (es. 15:00). Se l'utente chiede 'ora', 'adesso', 'subito', 'il prima possibile' oppure NON specifica alcun orario per lo spostamento, usa l'orario attuale '{cur_time_str}'. Se indica 'tra X minuti' o 'fra un'ora', calcola l'orario risultante sommando i minuti all'orario attuale ({cur_time_str})\",\n"
                "  \"is_arrival_time\": true se l'orario è l'arrivo desiderato (es. 'arrivare per le 15', 'essere a ... entro le 15'), false altrimenti,\n"
                "  \"transport\": \"QUALSIASI\" | \"TRENO\" | \"METRO\" | \"BUS\",\n"
                "  \"line_to_trace\": \"codice o nome linea se l'utente chiede quali fermate/percorso fa una linea (es. 'METRO', '23M', '5N', 'TRENO') oppure null\",\n"
                "  \"place_for_nearest_stop\": \"luogo o indirizzo se l'utente chiede solo qual è la fermata più vicina a un posto senza un viaggio A->B, oppure null\",\n"
                "  \"is_general_info\": true se è una richiesta generale (es. biglietti, pdf) senza luoghi o linee\n"
                "}"
            )
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "maxOutputTokens": 260,
                    "responseMimeType": "application/json",
                },
            }
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(raw_json)
        except Exception as e:
            logger.debug("LLM semantic extraction error: %s", e)
        return {}

    def _extract_rule_based(self, text: str, now: Optional[datetime] = None) -> ExtractedEntities:
        """Deterministic rule-based parser used as offline fallback."""
        origin = None
        destination = None
        ref_now = now or get_local_now()
        dep_time, has_explicit = resolve_query_time(text, ref_now)
        ttype = TransportType.QUALSIASI
        line_to_trace = None
        place_for_nearest = None

        lower_text = text.lower()
        if "metro" in lower_text or "metropolitana" in lower_text:
            ttype = TransportType.METRO
        elif "treno" in lower_text or "littorina" in lower_text or "ferrovia" in lower_text:
            ttype = TransportType.TRENO
        elif any(w in lower_text for w in ("bus", "autobus", "pullman", "navetta", "autolinee")):
            ttype = TransportType.BUS

        # Detect arrival vs departure intent
        is_arrival = bool(
            re.search(
                r"\b(?:arrivare|arrivo|arrivando|essere\s+(?:a|ad|in)|entro\s+le|per\s+le|prima\s+delle|entro)\b",
                text,
                re.IGNORECASE,
            )
        )
        ftype = FilterType.ARRIVAL_BEFORE if is_arrival else FilterType.DEPARTURE_AFTER

        # Detect line path tracing query (e.g. "che percorso fa la linea 23M?", "quali fermate fa la metro?")
        trace_match = re.search(
            r"(?:percorso|fermate|stazioni|giro|tratta|itinerario)\s+(?:(?:che\s+)?(?:fa|segue|effettua|tocca|percorre)\s+)?(?:della\s+|del\s+|la\s+|il\s+)?(?:linea\s+|navetta\s+|bus\s+)?([A-Za-z0-9]{1,5}|metropolitana|metro|treno|treni|littorina)\b",
            text,
            re.IGNORECASE,
        )
        if trace_match and not re.search(r"\bda\s+.+\s+(?:a|ad|per|verso)\s+", text, re.IGNORECASE):
            line_to_trace = trace_match.group(1).strip()

        # Detect nearest-stop query (e.g. "qual è la fermata più vicina a Piazza Duomo?")
        near_match = re.search(
            r"(?:fermata|stazione)\s+(?:fce\s+|metro\s+)?pi[uù]\s+vicin[ao]\s+(?:a|ad|al|allo|alla|in)\s+([^\?\.\!]+)",
            text,
            re.IGNORECASE,
        )
        if near_match:
            place_for_nearest = self._clean_entity(near_match.group(1))

        # Origin / Destination patterns (supporting accents, numbers in street addresses, apostrophes)
        loc_chars = r"[\w\'\’\s,\.àèéìòùÀÈÉÌÒÙ-]+"
        stop_kw = rf"(?:\s+{TIME_DELIMITERS_PATTERN}|\?|\.|!|$)"

        pat1 = re.search(
            rf"\b(?:da|dal|dalla|partendo\s+da)\s+({loc_chars}?)\s+(?:a|ad|al|allo|alla|in|verso|per(?!\s+(?:le\s+)?\d)\b|fino\s+a)\s+({loc_chars}?){stop_kw}",
            text,
            re.IGNORECASE,
        )
        if pat1:
            origin = self._clean_entity(pat1.group(1))
            destination = self._clean_entity(pat1.group(2))

        if not origin or not destination:
            pat2 = re.search(
                rf"\b(?:(?:ad\s+)?(?:arrivare|essere|andare)\s+(?:a|ad|in)|per|ad|a|al|allo|alla|in|verso)\s+({loc_chars}?)\s+(?:da|dal|dalla|partendo\s+da)\s+({loc_chars}?){stop_kw}",
                text,
                re.IGNORECASE,
            )
            if pat2:
                destination = self._clean_entity(pat2.group(1))
                origin = self._clean_entity(pat2.group(2))

        if not origin or not destination:
            pat3 = re.search(
                rf"^({loc_chars}?)\s*[-–>]\s*({loc_chars}?){stop_kw}",
                text.strip(),
                re.IGNORECASE,
            )
            if pat3:
                origin = self._clean_entity(pat3.group(1))
                destination = self._clean_entity(pat3.group(2))

        # Dictionary matching fallback
        if not origin and not destination and not line_to_trace and not place_for_nearest:
            found_stops = []
            norm_text = normalize_stop_name(text)
            for sname, norm_sname in self._get_known_stops():
                if len(norm_sname) >= 4 and f" {norm_sname} " in f" {norm_text} ":
                    if sname not in found_stops:
                        found_stops.append(sname)
            if len(found_stops) >= 2:
                origin = found_stops[0]
                destination = found_stops[1]

        return ExtractedEntities(
            origin,
            destination,
            dep_time,
            ttype,
            ftype,
            line_to_trace=line_to_trace,
            place_for_nearest_stop=place_for_nearest,
            has_explicit_time=has_explicit,
        )

    def extract_entities(self, text: str, now: Optional[datetime] = None) -> ExtractedEntities:
        """Extract origin, destination, time, transport mode, filter type, and path/proximity intents."""
        ref_now = now or get_local_now()
        rule_time, has_explicit = resolve_query_time(text, ref_now)

        if settings.GEMINI_API_KEY:
            llm_data = self._map_query_with_llm(text, now=ref_now)
            if llm_data:
                orig = self._clean_entity(llm_data.get("origin"))
                dest = self._clean_entity(llm_data.get("destination"))
                time_val = llm_data.get("time")
                if time_val and isinstance(time_val, str) and re.match(r"^[0-2]?[0-9]:[0-5][0-9]$", time_val.strip()):
                    parts = time_val.strip().split(":")
                    time_val = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
                else:
                    time_val = rule_time

                is_arr = bool(llm_data.get("is_arrival_time"))
                ftype = FilterType.ARRIVAL_BEFORE if is_arr else FilterType.DEPARTURE_AFTER
                raw_t = llm_data.get("transport") or "QUALSIASI"
                ttype = TransportType.from_str(str(raw_t))
                line_trace = llm_data.get("line_to_trace")
                place_near = self._clean_entity(llm_data.get("place_for_nearest_stop"))
                if orig or dest or line_trace or place_near or llm_data.get("is_general_info"):
                    return ExtractedEntities(
                        orig,
                        dest,
                        time_val,
                        ttype,
                        ftype,
                        line_to_trace=line_trace,
                        place_for_nearest_stop=place_near,
                        has_explicit_time=has_explicit,
                    )

        return self._extract_rule_based(text, now=ref_now)

    @staticmethod
    def _describe_resolution(role_label: str, res: Optional[LocationResolution]) -> str:
        """Build human-readable explanation when a stop was matched by similarity or address proximity."""
        if not res or not res.stop_ids:
            return ""
        if res.match_kind == "similar":
            return (
                f"{role_label}: \"{res.raw_query}\" associata per somiglianza alla fermata ufficiale "
                f"\"{res.resolved_label}\"."
            )
        if res.match_kind == "nearest_place":
            stops_desc = ", ".join(
                f"{s.display_name} (~{s.distance_meters} m)" if s.distance_meters is not None else s.display_name
                for s in res.matched_stops[:3]
            )
            return (
                f"{role_label}: per il luogo/indirizzo \"{res.resolved_label}\" le fermate più vicine sono: "
                f"{stops_desc}."
            )
        return ""

    def answer_query(self, user_question: str, now: Optional[datetime] = None) -> AIQueryResponse:
        """Process user natural language query with fuzzy stop matching, place geocoding, and path tracing."""
        ref_now = now or get_local_now()
        entities = self.extract_entities(user_question, now=ref_now)
        origin = entities.origin
        destination = entities.destination
        target_time = entities.time or ref_now.strftime("%H:%M")
        ttype = entities.transport_type
        ftype = entities.filter_type
        line_to_trace = entities.line_to_trace
        place_for_nearest = entities.place_for_nearest_stop

        orig_res: Optional[LocationResolution] = None
        dest_res: Optional[LocationResolution] = None
        itineraries = []
        next_day_itineraries = []
        traced_lines: List[dict] = []
        nearest_only_res: Optional[LocationResolution] = None

        # 1. Handle nearest-stop-to-place inquiry (when no origin->dest trip was requested)
        if place_for_nearest and not (origin and destination):
            nearest_only_res = transit_service.resolve_location(
                place_for_nearest, transport_type=ttype, allow_geocoding=True
            )
            if nearest_only_res.place_coords:
                traced_lines = transit_service.trace_line_stops(
                    "METRO" if ttype == TransportType.METRO else (ttype.value if ttype != TransportType.QUALSIASI else "METRO"),
                    near_coords=nearest_only_res.place_coords,
                    limit_routes=2,
                )

        # 2. Handle route / line path tracing inquiry
        if line_to_trace:
            near_pt = None
            if destination and not origin:
                geo = geocode_place_in_etna_area(destination, allow_network=True)
                if geo:
                    near_pt = (geo[0], geo[1])
            traced_lines = transit_service.trace_line_stops(line_to_trace, near_coords=near_pt, limit_routes=3)

        # 3. Handle Origin -> Destination trip planning (supporting exact, city, similar names, and addresses!)
        if origin and destination:
            orig_res = transit_service.resolve_location(origin, transport_type=ttype, allow_geocoding=True)
            dest_res = transit_service.resolve_location(destination, transport_type=ttype, allow_geocoding=True)

            # Multi-modal fallback: if strict mode (e.g. METRO) couldn't resolve a bus-served stop like Santa Sofia,
            # automatically broaden to QUALSIASI so Metro+Bus combinations are discovered!
            effective_ttype = ttype
            if (not orig_res.stop_ids or not dest_res.stop_ids) and ttype != TransportType.QUALSIASI:
                orig_any = transit_service.resolve_location(
                    origin, transport_type=TransportType.QUALSIASI, allow_geocoding=True
                )
                dest_any = transit_service.resolve_location(
                    destination, transport_type=TransportType.QUALSIASI, allow_geocoding=True
                )
                if orig_any.stop_ids and dest_any.stop_ids:
                    orig_res = orig_any
                    dest_res = dest_any
                    effective_ttype = TransportType.QUALSIASI

            if orig_res.stop_ids and dest_res.stop_ids:
                itineraries = transit_service.find_trips_from_resolutions(
                    origin_res=orig_res,
                    dest_res=dest_res,
                    target_time=target_time,
                    transport_type=effective_ttype,
                    filter_type=ftype,
                    limit=5,
                )
                # Broaden to multi-modal QUALSIASI if single mode had no connecting route
                if not itineraries and effective_ttype != TransportType.QUALSIASI:
                    itineraries = transit_service.find_trips_from_resolutions(
                        origin_res=orig_res,
                        dest_res=dest_res,
                        target_time=target_time,
                        transport_type=TransportType.QUALSIASI,
                        filter_type=ftype,
                        limit=5,
                    )
                # Fallback from ARRIVAL_BEFORE to DEPARTURE_AFTER if nothing arrives before early hour
                if not itineraries and ftype == FilterType.ARRIVAL_BEFORE:
                    itineraries = transit_service.find_trips_from_resolutions(
                        origin_res=orig_res,
                        dest_res=dest_res,
                        target_time=target_time,
                        transport_type=effective_ttype,
                        filter_type=FilterType.DEPARTURE_AFTER,
                        limit=5,
                    )
                # Next morning fallback if service ended for the day
                if not itineraries and ftype == FilterType.DEPARTURE_AFTER:
                    next_day_itineraries = transit_service.find_trips_from_resolutions(
                        origin_res=orig_res,
                        dest_res=dest_res,
                        target_time="05:00",
                        transport_type=effective_ttype,
                        filter_type=FilterType.DEPARTURE_AFTER,
                        limit=3,
                    )

        docs = documents_service.get_documents()
        resolved_orig_name = (orig_res.resolved_label if orig_res and orig_res.resolved_label else origin)
        resolved_dest_name = (dest_res.resolved_label if dest_res and dest_res.resolved_label else destination)
        display_itineraries = itineraries or next_day_itineraries

        if settings.GEMINI_API_KEY:
            try:
                llm_answer = self._call_gemini(
                    question=user_question,
                    origin=resolved_orig_name,
                    destination=resolved_dest_name,
                    orig_res=orig_res,
                    dest_res=dest_res,
                    nearest_only_res=nearest_only_res,
                    traced_lines=traced_lines,
                    target_time=target_time,
                    filter_type=ftype,
                    itineraries=itineraries,
                    docs=docs,
                    next_day_itineraries=next_day_itineraries,
                    now=ref_now,
                )
                if llm_answer:
                    return AIQueryResponse(
                        answer=llm_answer,
                        origin=resolved_orig_name,
                        destination=resolved_dest_name,
                        time=target_time,
                        itineraries_count=len(display_itineraries),
                        itineraries=self._serialize_itineraries(display_itineraries),
                    )
            except Exception as e:
                logger.error("Gemini call failed: %s, falling back to rule-based response", e)

        answer = self._generate_rule_based_answer(
            user_question=user_question,
            origin=resolved_orig_name,
            destination=resolved_dest_name,
            orig_res=orig_res,
            dest_res=dest_res,
            nearest_only_res=nearest_only_res,
            traced_lines=traced_lines,
            target_time=target_time,
            filter_type=ftype,
            itineraries=itineraries,
            docs=docs,
            next_day_itineraries=next_day_itineraries,
        )

        return AIQueryResponse(
            answer=answer,
            origin=resolved_orig_name,
            destination=resolved_dest_name,
            time=target_time,
            itineraries_count=len(display_itineraries),
            itineraries=self._serialize_itineraries(display_itineraries),
        )

    @staticmethod
    def _serialize_itineraries(itineraries: list) -> List[dict]:
        return [
            {
                "departure": it.departure_time,
                "arrival": it.arrival_time,
                "from": it.departure_stop,
                "to": it.arrival_stop,
                "direct": it.is_direct,
                "segments": [s.to_dict() for s in it.segments],
            }
            for it in itineraries
        ]

    def _build_resolution_notes(
        self,
        orig_res: Optional[LocationResolution],
        dest_res: Optional[LocationResolution],
    ) -> List[str]:
        notes = []
        n1 = self._describe_resolution("Partenza", orig_res)
        if n1:
            notes.append(f"📍 {n1}")
        n2 = self._describe_resolution("Destinazione", dest_res)
        if n2:
            notes.append(f"🏁 {n2}")
        return notes

    def _generate_rule_based_answer(
        self,
        user_question: str,
        origin: Optional[str],
        destination: Optional[str],
        orig_res: Optional[LocationResolution],
        dest_res: Optional[LocationResolution],
        nearest_only_res: Optional[LocationResolution],
        traced_lines: List[dict],
        target_time: str,
        filter_type: FilterType,
        itineraries: list,
        docs: Any,
        next_day_itineraries: Optional[list] = None,
    ) -> str:
        """Fallback response generator providing clean text without broken markdown."""
        q_low = user_question.lower()

        if any(w in q_low for w in ["documenti", "pdf", "scarica", "tabella orari", "orario metro", "orario treni", "orario bus"]):
            return (
                "Puoi consultare e scaricare gli orari aggiornati ufficiali di Metropolitana, Treni di superficie e Autolinee Bus direttamente nella sezione dedicata o sul sito circumetnea.it.\n\n"
                "Se desideri cercare una corsa specifica, indicami pure partenza, destinazione (anche un indirizzo o luogo!) e l'orario desiderato!"
            )

        if any(w in q_low for w in ["bigliett", "ticket", "abbonament", "compra", "acquist", "tariffe", "costo"]):
            return (
                "I biglietti e gli abbonamenti della Ferrovia Circumetnea e della Metropolitana di Catania sono acquistabili:\n\n"
                "• Presso le biglietterie di stazione e le rivendite autorizzate (tabacchi ed edicole convenzionate).\n"
                "• Alle emettitrici automatiche attive in tutte le stazioni della metropolitana.\n"
                "• Tramite smartphone con l'app ufficiale o l'app DropTicket.\n"
                "• A bordo per le corse dei bus extraurbani (ove previsto).\n\n"
                "Puoi consultare e scaricare gli orari aggiornati ufficiali nella sezione dedicata."
            )

        # Nearest stop to a place/address
        if nearest_only_res and nearest_only_res.matched_stops:
            lines = [
                f"📍 Fermate più vicine a {nearest_only_res.resolved_label or nearest_only_res.raw_query}:\n"
            ]
            for s in nearest_only_res.matched_stops[:5]:
                modes = []
                if s.has_metro:
                    modes.append("Metro")
                if s.has_train:
                    modes.append("Treno")
                if s.has_bus:
                    modes.append("Bus")
                dist_txt = f" — a circa {s.distance_meters} m" if s.distance_meters is not None else ""
                lines.append(f"• {s.display_name} ({', '.join(modes)}){dist_txt}")
            lines.append("\nScrivimi anche da dove vuoi partire e a che ora per calcolare subito il percorso completo!")
            return "\n".join(lines)

        # Line / route path tracing
        if traced_lines and not (origin and destination):
            lines = ["🗺 Percorso delle fermate rilevato:\n"]
            for tl in traced_lines:
                head = f" (Direzione {tl['headsign']})" if tl.get("headsign") else ""
                lines.append(f"• Linea {tl['route_name']} - {tl['transport_type']}{head}:")
                lines.append("  " + " -> ".join(tl["stops"]))
                if tl.get("closest_stop_to_target"):
                    cs = tl["closest_stop_to_target"]
                    lines.append(f"  📍 Fermata più vicina al punto indicato: {cs['stop_name']} (~{cs['distance_meters']} m)")
            return "\n".join(lines)

        if not origin or not destination:
            return (
                "👋 Ciao! Sono l'assistente per la mobilità della Ferrovia Circumetnea e Metropolitana di Catania.\n\n"
                "Puoi chiedermi orari, fermate vicine a un indirizzo o il percorso delle linee, ad esempio:\n"
                "• Come vado da Catania Santa Sofia a Stesicoro verso le 09:00?\n"
                "• Come arrivo in Piazza Duomo Catania partendo da Paternò alle 08:30?\n"
                "• Qual è la fermata più vicina al Policlinico di Catania?\n"
                "• Quali fermate fa la metropolitana?\n\n"
                "Dimmi pure: da dove vuoi partire e dove desideri arrivare?"
            )

        notes = self._build_resolution_notes(orig_res, dest_res)
        notes_block = ("\n".join(notes) + "\n\n") if notes else ""

        if not itineraries:
            desc = (
                f"con arrivo entro le ore {target_time}"
                if filter_type == FilterType.ARRIVAL_BEFORE
                else f"a partire dalle ore {target_time}"
            )
            if next_day_itineraries:
                m_lines = [f"{idx}. {it.format_clean(include_path=True)}" for idx, it in enumerate(next_day_itineraries[:3], 1)]
                return (
                    f"{notes_block}Non ci sono ulteriori corse disponibili da {origin} a {destination} {desc} (il servizio potrebbe essere terminato per oggi).\n\n"
                    "🌅 Prime corse disponibili alla ripresa del servizio (mattina):\n"
                    + "\n".join(m_lines)
                )
            return (
                f"{notes_block}Non ho trovato corse dirette o con cambio ravvicinato da {origin} a {destination} {desc}.\n\n"
                "Suggerimenti:\n"
                "• Prova a indicare una fascia oraria diversa (es. mattina o primo pomeriggio).\n"
                "• Puoi consultare gli orari aggiornati ufficiali nella sezione dedicata."
            )

        desc = (
            f"con arrivo entro le ore {target_time}"
            if filter_type == FilterType.ARRIVAL_BEFORE
            else f"a partire dalle ore {target_time}"
        )
        lines = []
        if notes:
            lines.extend(notes)
            lines.append("")
        lines.append(f"Ho trovato {len(itineraries)} soluzioni da {origin} a {destination} {desc}:\n")
        for idx, it in enumerate(itineraries[:4], 1):
            lines.append(f"{idx}. {it.format_clean(include_path=True)}")

        return "\n".join(lines)

    def _call_gemini(
        self,
        question: str,
        origin: Optional[str],
        destination: Optional[str],
        orig_res: Optional[LocationResolution],
        dest_res: Optional[LocationResolution],
        nearest_only_res: Optional[LocationResolution],
        traced_lines: List[dict],
        target_time: str,
        filter_type: FilterType,
        itineraries: list,
        docs: Any,
        next_day_itineraries: Optional[list] = None,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Call Google Gemini API with full resolution, proximity, and route-path context."""
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            return None

        extra_ctx_parts: List[str] = []
        res_notes = self._build_resolution_notes(orig_res, dest_res)
        if res_notes:
            extra_ctx_parts.append("CORRISPONDENZA FERMATE E VICINANZA LUOGHI:\n" + "\n".join(res_notes))

        if nearest_only_res and nearest_only_res.matched_stops:
            near_lines = [
                f"- {s.display_name} (distanza ~{s.distance_meters} m)"
                for s in nearest_only_res.matched_stops[:5]
            ]
            extra_ctx_parts.append(
                f"FERMATE PIÙ VICINE A '{nearest_only_res.resolved_label or nearest_only_res.raw_query}':\n"
                + "\n".join(near_lines)
            )

        if traced_lines:
            tl_parts = []
            for tl in traced_lines:
                s_str = " -> ".join(tl["stops"])
                line_info = f"Linea {tl['route_name']} ({tl['transport_type']}): {s_str}"
                if tl.get("closest_stop_to_target"):
                    cs = tl["closest_stop_to_target"]
                    line_info += f" | Fermata più vicina al punto: {cs['stop_name']} (~{cs['distance_meters']} m)"
                tl_parts.append(line_info)
            extra_ctx_parts.append("TRACCIATO FERMATE LINEE:\n" + "\n".join(tl_parts))

        if itineraries:
            schedule_ctx = "ORARI E PERCORSI TROVATI DAL DATABASE GTFS:\n"
            for it in itineraries:
                schedule_ctx += it.format_clean(include_path=True) + "\n"
        elif next_day_itineraries:
            schedule_ctx = (
                f"Nessuna corsa trovata per l'orario richiesto ({target_time}) poiché il servizio per oggi è terminato.\n"
                f"PRIME CORSE DISPONIBILI ALLA RIPRESA DEL SERVIZIO (MATTINA):\n"
            )
            for it in next_day_itineraries:
                schedule_ctx += it.format_clean(include_path=True) + "\n"
        else:
            schedule_ctx = "Nessuna corsa trovata dal database per i criteri specificati."

        criteria_str = (
            f"Arrivo richiesto entro le {target_time}"
            if filter_type == FilterType.ARRIVAL_BEFORE
            else f"Partenza richiesta a partire dalle ore {target_time}"
        )
        extra_ctx = ("\n\n".join(extra_ctx_parts) + "\n\n") if extra_ctx_parts else ""
        ref_now = now or get_local_now()
        cur_now_str = ref_now.strftime("%H:%M")

        prompt = f"""Domanda del passeggero: "{question}"
Origine individuata: {origin or 'Non specificata'}
Destinazione individuata: {destination or 'Non specificata'}
Ora attuale locale: {cur_now_str}
Criterio orario: {criteria_str}

{extra_ctx}{schedule_ctx}

Linee guida per la risposta:
- Rispondi in italiano con tono cordiale, chiaro e conciso.
- Se una fermata è stata trovata per somiglianza (es. Parcheggio Santa Sofia) o come fermata più vicina a un indirizzo/luogo (indicando i metri approssimativi a piedi), spiegalo chiaramente all'utente.
- Indica gli orari migliori e cita sinteticamente il percorso/cambio effettuato dalla linea.
- Se il passeggero chiedeva di partire subito/il prima possibile o non ha indicato un orario, proponi le prime corse in partenza rispetto all'orario attuale ({cur_now_str}).
- Se il servizio per oggi è terminato, avvisa cordialmente che le corse indicate si riferiscono alla ripresa del servizio la mattina successiva.
- NON inserire URL completi e NON usare link markdown grezzi come [testo](url).
"""

        model = settings.GEMINI_MODEL or "gemini-3.5-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n" + prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        logger.warning("Gemini API error (%d): %s", resp.status_code, resp.text[:200])
        return None


ai_service = AIService()
