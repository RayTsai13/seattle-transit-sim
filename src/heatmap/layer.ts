import type { LayerProps } from "react-map-gl/maplibre";

export const HEATMAP_LAYER_ID = "foot-traffic-heatmap";

export const heatmapLayer: LayerProps = {
  id: HEATMAP_LAYER_ID,
  beforeId: "transit-line-casing",
  type: "circle",
  layout: {
    "circle-sort-key": ["get", "density"],
  },
  paint: {
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      10,
      18,
      13,
      34,
      15,
      52,
      16,
      68,
    ],

    "circle-color": [
      "interpolate",
      ["linear"],
      ["get", "density"],
      0,
      "rgba(0, 0, 0, 0)",
      0.05,
      "rgba(49, 54, 149, 0.21)",
      0.1,
      "rgba(69, 117, 180, 0.38)",
      0.2,
      "rgba(116, 173, 209, 0.55)",
      0.3,
      "rgba(171, 217, 233, 0.64)",
      0.4,
      "rgba(224, 243, 248, 0.68)",
      0.5,
      "rgba(255, 255, 191, 0.72)",
      0.6,
      "rgba(254, 224, 144, 0.72)",
      0.7,
      "rgba(253, 174, 97, 0.77)",
      0.8,
      "rgba(244, 109, 67, 0.78)",
      0.9,
      "rgba(215, 48, 39, 0.81)",
      1.0,
      "rgba(165, 0, 38, 0.85)",
    ],
    "circle-blur": 0.75,
    "circle-opacity": 0.82,
  },
};
