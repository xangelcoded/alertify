// Ticker messages (same as alerts)
const tickerMessages = [
  "[AL-2025-001] Sabang • FLOODING IN THE AREA • CRITICAL",
  "[AL-2025-002] Pinagtongulan • LANDSLIDE RISK • HIGH",
  "[AL-2025-003] JP Laurel • FIRE IN THE AREA • MODERATE",
  "[AL-2025-004] Balintawak • STRONG WINDS / TYPHOON • HIGH",
  "[AL-2025-005] Marawoy • IMPASSABLE ROADS • CRITICAL",
  "[AL-2025-006] Poblacion • MEDICAL EMERGENCY • HIGH",
  "[AL-2025-007] Mataasnakahoy Rd • MINOR FLOODING • MODERATE",
  "[AL-2025-008] Lodlod • TRAPPED RESIDENTS • CRITICAL",
  "[AL-2025-009] San Carlos • POWER INTERRUPTION • HIGH",
  "[AL-2025-010] Niyugan River • RIVER LEVEL RISING • MODERATE"
];

function startTicker() {
  const tickerText = document.getElementById("ticker-text");
  if (!tickerText) return;

  let i = 0;
  function update() {
    tickerText.textContent = tickerMessages[i % tickerMessages.length];
    i++;
  }
  update();
  setInterval(update, 4000);
}

function initMap() {
  const mapContainer = document.getElementById("map");
  if (!mapContainer || typeof L === "undefined") return;

  const LIPA_CENTER = [13.941, 121.163];

  const map = L.map("map").setView(LIPA_CENTER, 13);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors"
  }).addTo(map);

  const alerts = [
    { coords: [13.936, 121.170], label: "Flooding - Brgy. Sabang", urgency: "CRITICAL" },
    { coords: [13.951, 121.162], label: "Landslide risk - Brgy. Pinagtongulan", urgency: "HIGH" },
    { coords: [13.939, 121.154], label: "Fire contained - JP Laurel", urgency: "MODERATE" },
    { coords: [13.947, 121.176], label: "Strong winds - Brgy. Balintawak", urgency: "HIGH" },
    { coords: [13.956, 121.150], label: "Road impassable - Brgy. Marawoy", urgency: "CRITICAL" },
    { coords: [13.941, 121.164], label: "Medical case - Poblacion", urgency: "HIGH" },
    { coords: [13.949, 121.150], label: "Street flooding - Mataasnakahoy Rd", urgency: "MODERATE" },
    { coords: [13.927, 121.169], label: "Trapped residents - Brgy. Lodlod", urgency: "CRITICAL" },
    { coords: [13.959, 121.182], label: "Power outage - Brgy. San Carlos", urgency: "HIGH" },
    { coords: [13.932, 121.158], label: "River level rising - Niyugan River", urgency: "MODERATE" }
  ];

  alerts.forEach(a => {
    let color = "#37d299";
    if (a.urgency === "CRITICAL") color = "#ff1744";
    else if (a.urgency === "HIGH") color = "#ffb020";

    L.circleMarker(a.coords, {
      radius: 9,
      color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.5
    })
      .addTo(map)
      .bindPopup(
        `<div style="font-family: Inter, system-ui; font-size:13px;">
          <strong>${a.label}</strong><br/>
          <span style="font-size:11px;">Urgency: ${a.urgency}</span>
        </div>`
      );
  });
}

document.addEventListener("DOMContentLoaded", () => {
  startTicker();
  initMap();
});
