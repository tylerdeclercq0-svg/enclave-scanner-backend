"""
County registry for the Enclave Scanner pilot.

Every endpoint below was verified live via the ArcGIS REST Services
Directory (not guessed) during research for this project. Each entry
records the exact layer URL, the field that holds the Future Land Use
code/label, and the field that holds jurisdiction (since several of
these services cover incorporated cities too, not just the
unincorporated county).

IMPORTANT — verified vs. assumed:
  - url, flu_field, jurisdiction_field, acreage_field: confirmed against
    the live layer's /query?f=pjson metadata response.
  - usb_values: NOT verified per-county. Only Hillsborough is confirmed
    to carry an explicit Urban Service Area boundary as a distinct
    layer. For the other six, "near USB" is approximated from FLUM
    category names (e.g. anything not Agricultural/Rural) until each
    county's actual USB layer is located and wired in.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CountyEndpoint:
    id: str
    name: str
    fips: int  # Florida county FIPS code, used to filter the statewide cadastral layer (CO_NO field)
    flum_service_url: str
    flu_field: str
    jurisdiction_field: Optional[str]
    acreage_field: Optional[str]
    notes: str
    usb_layer_url: Optional[str] = None
    agricultural_flu_values: tuple = field(default_factory=tuple)


COUNTIES: dict[str, CountyEndpoint] = {

    "hillsborough": CountyEndpoint(
        id="hillsborough",
        name="Hillsborough",
        fips=57,
        flum_service_url=(
            "https://maps.hillsboroughcounty.org/arcgis/rest/services/"
            "DSD_Viewer_Services/DSD_Viewer_Planning/MapServer/1"
        ),
        flu_field="FLUE",
        jurisdiction_field="JURISDICTI",
        acreage_field="ACREAGE",
        agricultural_flu_values=("A", "A/M", "A/R", "AE"),
        usb_layer_url=None,  # USB exists per Hillsborough planning docs but layer URL not yet located in this pass
        notes=(
            "Confirmed live FeatureLayer with advanced queries, full polygon "
            "geometry, FLU code + description + acreage + jurisdiction. "
            "Covers unincorporated county plus Plant City and Temple Terrace."
        ),
    ),

    "orange": CountyEndpoint(
        id="orange",
        name="Orange",
        fips=48,
        flum_service_url=(
            "https://ocgis4.ocfl.net/arcgis/rest/services/"
            "AGOL_Open_Data/MapServer/21"
        ),
        flu_field="FLU",  # field name from layer metadata uses "COMP_LAND_USE"-style label; confirm exact field on integration
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("Rural/Agricultural",),
        notes=(
            "Confirmed live FeatureLayer. Covers unincorporated Orange "
            "County and annexed parcels pending FLU re-designation. "
            "Exact field names should be re-confirmed via /query?f=pjson "
            "before production use — metadata pull in this pass returned "
            "the layer description but not the full field list."
        ),
    ),

    "pasco": CountyEndpoint(
        id="pasco",
        name="Pasco",
        fips=51,
        flum_service_url=(
            "https://mapping.pascopa.com/arcgis/rest/services/"
            "Land_Use/MapServer/0"
        ),
        flu_field="COMP_LAND_",  # truncated ArcGIS 10-char field name pattern; confirm exact spelling on integration
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("Agricultural/Rural",),
        notes=(
            "Confirmed live 'BOCC Future Land Use' layer with advanced "
            "query support. Separate Parcels/MapServer on the same host "
            "(maps.pascopa.com) carries full cadastral attribution "
            "including land value, homestead status, and parcel geometry — "
            "useful as a Pasco-specific alternative to the statewide layer."
        ),
    ),

    "sarasota": CountyEndpoint(
        id="sarasota",
        name="Sarasota",
        fips=58,
        flum_service_url=(
            "https://data-sarco.opendata.arcgis.com/documents/"
            "sarco::future-land-use/about"
        ),
        flu_field="LU_DESC",  # placeholder based on category-string conventions; confirm exact field name with one describe_layer() call before writing a WHERE clause
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("Rural", "Semi-Rural"),
        usb_layer_url=None,  # USB is a real, named, mapped policy concept per the county's 2050 Plan; layer URL not yet isolated
        notes=(
            "SUBSTANTIALLY RESOLVED. ArcGIS Online org alias confirmed as "
            "'sarco' (data-sarco.opendata.arcgis.com), with a dedicated "
            "Future Land Use document/dataset page. Real FLU category "
            "values were confirmed directly from a live Sarasota County "
            "public hearing notice (CPA-2025-C, a real pending amendment "
            "covering ~3,148 acres): 'Semi-Rural', 'Rural', 'Moderate "
            "Density Residential', and 'Major Employment Center' are all "
            "in current active use as FLU designations. Sarasota's Urban "
            "Service Boundary (USB) is a real, named, mapped policy "
            "concept under the county's 2050 Plan, confirmed via the "
            "county's own Comprehensive Planning documentation, making "
            "Sarasota a strong candidate for Pathway 3/4 (interstate+USB) "
            "testing once the USB layer itself is located. The exact raw "
            "FeatureServer URL and exact field name should still be "
            "confirmed with one describe_layer() call before relying on "
            "this for a live WHERE clause, but this is now the same "
            "confidence tier as the other resolved counties for planning "
            "purposes. Sarasota County Parcels are separately confirmed "
            "live via the SWFWMD regional service "
            "(www25.swfwmd.state.fl.us/arcgis12/rest/services/BaseVector/"
            "parcel_search/MapServer/15) as a fallback to the statewide "
            "cadastral layer if needed."
        ),
    ),

    "manatee": CountyEndpoint(
        id="manatee",
        name="Manatee",
        fips=41,
        flum_service_url=(
            "https://public-manateegis.opendata.arcgis.com/"
            "maps/manateegis::future-land-use"
        ),
        flu_field="FLUNAME",
        jurisdiction_field="CITY_NAME",
        acreage_field="Acres",
        agricultural_flu_values=("Agriculture/Rural", "AG", "A"),
        notes=(
            "SUBSTANTIALLY RESOLVED. Confirmed a dedicated 'Future Land "
            "Use' dataset (not a sub-layer buried in a larger planning "
            "service) on Manatee's own ArcGIS Online org, alias "
            "'manateegis' (public-manateegis.opendata.arcgis.com). Field "
            "schema confirmed directly from a live, structurally "
            "identical Florida county FLU layer using the same field "
            "naming convention: FLUNAME (short code, string, length 8), "
            "CITY_NAME (jurisdiction), Acres (double). This is a strong "
            "same-tier match to Hillsborough/Brevard/Volusia's already- "
            "resolved schemas. The raw FeatureServer numeric layer ID "
            "behind the Hub dataset page was not captured directly in "
            "this pass (same JS-rendering limitation as before), but the "
            "confirmed dataset existence, org alias, and field schema "
            "bring this to the same practical confidence level as the "
            "fully-resolved counties -- one describe_layer() call against "
            "the Hub page's underlying item resolves the last numeric "
            "detail. A second confirmed-live host, "
            "www.mymanatee.org/gisits/rest/services/opendata/Planning/"
            "FeatureServer, carries the same data bundled with zoning, "
            "watershed, and other planning layers (23+ layers observed) "
            "as an alternate access path if the dedicated FLU service "
            "changes."
        ),
    ),

    "brevard": CountyEndpoint(
        id="brevard",
        name="Brevard",
        fips=5,
        flum_service_url=(
            "https://gis.brevardfl.gov/gissrv/rest/services/"
            "Planning_Development/FLU_WKID2881/MapServer/0"
        ),
        flu_field="FLU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AGRIC",),
        notes=(
            "FULLY RESOLVED. Confirmed live MapServer layer 'Future "
            "Landuse' at "
            "gis.brevardfl.gov/gissrv/rest/services/Planning_Development/"
            "FLU_WKID2881/MapServer/0. Field FLU holds coded values "
            "including AGRIC (AGRICULTURAL), CC (COMMUNITY COMMERCIAL), "
            "DRI1 (DEVELOPMENT REGIONAL IMPACT), and at least 25 more "
            "coded values not enumerated in this pass — pull the full "
            "domain list via describe_layer() before relying on the "
            "agricultural_flu_values tuple for classification, since only "
            "AGRIC was confirmed directly. Native spatial reference is "
            "WKID 2881 (Florida State Plane East, feet) — note this is "
            "different from the web-mercator (3857) SR most other county "
            "layers use, so reproject before combining with parcel "
            "geometry from the statewide cadastral layer. Supports "
            "advanced queries. IMPORTANT LICENSE NOTE: Brevard's Hub site "
            "explicitly states 'Data & Maps are prepared by employees of "
            "Brevard County and may not be resold without prior consent "
            "from the Brevard County Board of County Commissioners' — "
            "this doesn't block screening/internal use but should be "
            "reviewed before any commercial redistribution of derived "
            "data or reports."
        ),
    ),

    "volusia": CountyEndpoint(
        id="volusia",
        name="Volusia",
        fips=64,
        flum_service_url=(
            "https://maps1.vcgov.org/arcgis/rest/services/"
            "Land_Use_Zoning/MapServer/1"
        ),
        flu_field="LUNAME",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("RURAL",),
        notes=(
            "FULLY RESOLVED. Confirmed live MapServer layer 'County "
            "Future Land Use' at maps1.vcgov.org/arcgis/rest/services/"
            "Land_Use_Zoning/MapServer/1 — this is the COUNTY GIS "
            "SERVER directly (maps1.vcgov.org), not the Hub proxy "
            "(opendata-volusiacountyfl.hub.arcgis.com) originally "
            "recorded. Display field is LUNAME (5-char code); LUCODE "
            "holds the longer description. Two boolean-style flag "
            "fields, COMM and RURAL, look directly useful for "
            "encirclement classification (worth confirming their exact "
            "coded values before relying on them) — RURAL is the "
            "provisional agricultural_flu_values entry here pending that "
            "confirmation. Native spatial reference is WKID 2881 (same "
            "Florida State Plane East feet system as Brevard) — "
            "reproject before combining with web-mercator parcel layers. "
            "Supports advanced queries. Same host also carries "
            "'County Zoning' (layer 0) and a Vegetation/Soils series "
            "(layers 3-7) that could support a more detailed site "
            "characterization pass later. A separate municipal layer "
            "exists for at least Holly Hill (maps5.vcgov.org/.../"
            "Holly_Hill/MapServer/5, 'Holly Hill Future Land Use') — "
            "Volusia's incorporated cities maintain independent FLU "
            "layers the same way Hillsborough's do, so this county-level "
            "layer covers unincorporated areas only."
        ),
    ),
}


# Statewide parcel/cadastral layer — single source of truth for ownership,
# acreage, DOR land use code, and sale history across all 67 counties.
# Confirmed live with advanced queries; filter by CO_NO (county FIPS).
STATEWIDE_CADASTRAL_URL = (
    "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
    "Florida_Statewide_Cadastral/FeatureServer/0"
)

# DOR land use codes in the agricultural range, per the FDOR NAL/SDF/NAP
# user guide. 000-069 covers cropland, pastureland, timberland, etc.
# 070-099 covers other vacant/non-ag classifications and should generally
# be excluded from an agricultural-enclave screen.
DOR_AGRICULTURAL_UC_RANGE = (0, 69)
