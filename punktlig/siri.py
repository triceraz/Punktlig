"""Parse SIRI-ET (Estimated Timetable) XML from Entur into plain dicts.

Each EstimatedVehicleJourney carries the journey's identity plus a list of calls:
RecordedCalls (stops already passed, containing the ground truth) and
EstimatedCalls (stops ahead, containing Entur's live predictions).
"""

import xml.etree.ElementTree as ET

SIRI_NS = "http://www.siri.org.uk/siri"
NS = {"s": SIRI_NS}


def _text(el, path):
    v = el.findtext(path, default=None, namespaces=NS)
    return v.strip() if v else None


def _flag(el, path):
    return 1 if (_text(el, path) or "").lower() == "true" else 0


def parse_sx(xml_bytes):
    """Return the deviation situations in a SIRI-SX response.

    One dict per PtSituationElement, with the affected line references
    flattened into a list. Stops are ignored for now: the model works at line
    level, and a situation without a line reference is network-wide anyway.
    """
    root = ET.fromstring(xml_bytes)
    situations = []
    for el in root.iter(f"{{{SIRI_NS}}}PtSituationElement"):
        lines = [
            ref.text.strip()
            for ref in el.iter(f"{{{SIRI_NS}}}LineRef")
            if ref.text and ref.text.strip()
        ]
        situations.append(
            {
                "situation_number": _text(el, "s:SituationNumber"),
                "created_at": _text(el, "s:CreationTime"),
                "progress": _text(el, "s:Progress"),
                "severity": _text(el, "s:Severity"),
                "report_type": _text(el, "s:ReportType"),
                "start_time": _text(el, "s:ValidityPeriod/s:StartTime"),
                "end_time": _text(el, "s:ValidityPeriod/s:EndTime"),
                "line_refs": sorted(set(lines)),
            }
        )
    return situations


def parse_et(xml_bytes):
    """Return (journeys, more_data). Each journey dict has a 'calls' list."""
    root = ET.fromstring(xml_bytes)
    more = (root.findtext(".//s:MoreData", default="false", namespaces=NS) or "").lower() == "true"

    journeys = []
    for ej in root.iter(f"{{{SIRI_NS}}}EstimatedVehicleJourney"):
        journey = {
            "recorded_at": _text(ej, "s:RecordedAtTime"),
            "line_ref": _text(ej, "s:LineRef"),
            "direction": _text(ej, "s:DirectionRef"),
            "journey_ref": _text(ej, "s:FramedVehicleJourneyRef/s:DatedVehicleJourneyRef")
            or _text(ej, "s:EstimatedVehicleJourneyCode"),
            "operating_date": _text(ej, "s:FramedVehicleJourneyRef/s:DataFrameRef"),
            "operator_ref": _text(ej, "s:OperatorRef"),
            "monitored": _flag(ej, "s:Monitored"),
            "cancelled": _flag(ej, "s:Cancellation"),
            "calls": [],
        }
        for parent, call_type in (
            ("s:RecordedCalls/s:RecordedCall", "recorded"),
            ("s:EstimatedCalls/s:EstimatedCall", "estimated"),
        ):
            for call in ej.findall(parent, NS):
                order = _text(call, "s:Order")
                journey["calls"].append(
                    {
                        "call_type": call_type,
                        "stop_ref": _text(call, "s:StopPointRef"),
                        "stop_name": _text(call, "s:StopPointName"),
                        "order_no": int(order) if order else None,
                        "aimed_arr": _text(call, "s:AimedArrivalTime"),
                        "expected_arr": _text(call, "s:ExpectedArrivalTime"),
                        "actual_arr": _text(call, "s:ActualArrivalTime"),
                        "aimed_dep": _text(call, "s:AimedDepartureTime"),
                        "expected_dep": _text(call, "s:ExpectedDepartureTime"),
                        "actual_dep": _text(call, "s:ActualDepartureTime"),
                        "call_cancelled": _flag(call, "s:Cancellation"),
                    }
                )
        journeys.append(journey)
    return journeys, more
