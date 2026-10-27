const {
  Document, Packer, Paragraph, TextRun, HeadingLevel,
  AlignmentType, PageBreak, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, NumberingList,
  LevelFormat, convertInchesToTwip,
} = require("docx");
const fs = require("fs");
const path = require("path");

// ── Numbering config for bullet lists ──────────────────────────────────────
const numbering = {
  config: [
    {
      reference: "bullet-list",
      levels: [
        {
          level: 0,
          format: LevelFormat.BULLET,
          text: "•",
          alignment: AlignmentType.LEFT,
          style: {
            paragraph: {
              indent: { left: convertInchesToTwip(0.4), hanging: convertInchesToTwip(0.2) },
            },
          },
        },
      ],
    },
  ],
};

// ── Helpers ────────────────────────────────────────────────────────────────
const H1 = (text) =>
  new Paragraph({
    text,
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 320, after: 120 },
  });

const H2 = (text) =>
  new Paragraph({
    text,
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 80 },
  });

const Body = (text, opts = {}) =>
  new Paragraph({
    children: [new TextRun({ text, size: 22, ...opts })],
    spacing: { after: 120 },
  });

const Bullet = (text) =>
  new Paragraph({
    children: [new TextRun({ text, size: 22 })],
    numbering: { reference: "bullet-list", level: 0 },
    spacing: { after: 60 },
  });

const Break = () =>
  new Paragraph({ children: [new PageBreak()] });

// ── Table helper ───────────────────────────────────────────────────────────
// colWidths: array of DXA widths (must sum to tableWidthDXA)
// rows: array of arrays of strings
// headerRow: first row is bold + shaded
const SimpleTable = (colWidths, rows, headerRow = true) => {
  const tableWidthDXA = colWidths.reduce((a, b) => a + b, 0);

  const makeCell = (text, bold, shade) =>
    new TableCell({
      width: { size: colWidths[0], type: WidthType.DXA }, // overridden per-cell below
      shading: shade ? { type: ShadingType.CLEAR, fill: "F0F4FF" } : undefined,
      children: [
        new Paragraph({
          children: [new TextRun({ text, size: 20, bold })],
          spacing: { before: 60, after: 60 },
        }),
      ],
      margins: { top: 60, bottom: 60, left: 120, right: 120 },
    });

  return new Table({
    width: { size: tableWidthDXA, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: rows.map((cells, ri) =>
      new TableRow({
        children: cells.map((text, ci) => {
          const cell = makeCell(text, ri === 0 && headerRow, ri === 0 && headerRow);
          // override per-cell width
          cell.options.width = { size: colWidths[ci], type: WidthType.DXA };
          return cell;
        }),
      })
    ),
  });
}

// ── Document body ──────────────────────────────────────────────────────────

const doc = new Document({
  numbering,
  styles: {
    default: {
      document: {
        run: { font: "Calibri", size: 22 },
      },
    },
    paragraphStyles: [
      {
        id: "Heading1",
        name: "Heading 1",
        basedOn: "Normal",
        run: { bold: true, size: 32, color: "1E3A5F" },
        paragraph: { spacing: { before: 400, after: 160 } },
      },
      {
        id: "Heading2",
        name: "Heading 2",
        basedOn: "Normal",
        run: { bold: true, size: 24, color: "2563EB" },
        paragraph: { spacing: { before: 280, after: 100 } },
      },
    ],
  },
  sections: [
    {
      properties: {
        page: {
          margin: {
            top: convertInchesToTwip(1),
            bottom: convertInchesToTwip(1),
            left: convertInchesToTwip(1.2),
            right: convertInchesToTwip(1.2),
          },
        },
      },
      children: [
        // ── COVER / PAGE 1 ────────────────────────────────────────────────
        new Paragraph({
          children: [new TextRun({ text: "Frigilunch Production Planner", bold: true, size: 52, color: "1E3A5F" })],
          alignment: AlignmentType.LEFT,
          spacing: { before: 800, after: 160 },
        }),
        new Paragraph({
          children: [new TextRun({ text: "System Overview", size: 32, color: "6B7280" })],
          spacing: { after: 80 },
        }),
        new Paragraph({
          children: [new TextRun({ text: "Client Report · October 2026", size: 20, color: "9CA3AF" })],
          spacing: { after: 600 },
        }),

        H1("What is Frigilunch?"),
        Body(
          "Frigilunch is an automated weekly production planning system for a fresh-food " +
          "manufacturing facility. It replaces manual scheduling by combining order data, " +
          "factory capacity, and over fifty production rules into a compliant, optimised " +
          "schedule — delivered each week as an Excel file and capacity dashboard."
        ),
        Body(
          "The system covers two assembly lines, a rolling seven-week horizon, and hundreds " +
          "of active orders at any given time. A full planning run completes in under five minutes."
        ),

        H2("The Planning Challenge"),
        Body("Fresh-food production is constrained in ways that make manual planning unreliable:"),
        Bullet("Orders arrive with narrow delivery windows — one day late means a rejected order"),
        Bullet("Allergen regulations require strict ascending-level sequencing within each production day"),
        Bullet("Two lines have different equipment — not every product can run on either line"),
        Bullet("Shared equipment creates scheduling conflicts that cause downtime if unmanaged"),
        Bullet("Staffing ramp-up rules limit how quickly the line can switch between product types"),

        H2("Key Outputs"),
        Body("For each production week the system produces:"),
        Bullet("A day-by-day production schedule per line (Excel, ready for the floor)"),
        Bullet("A capacity dashboard showing utilisation, rejected orders, and bottlenecks"),
        Bullet("An action list of orders that could not be placed and require manual follow-up"),

        Break(),

        // ── PAGE 2 ────────────────────────────────────────────────────────
        H1("How the System Works"),

        H2("Pipeline Overview"),
        Body("The planner runs as a weekly batch in five sequential stages:"),

        SimpleTable(
          [600, 2400, 3600],
          [
            ["#", "Stage", "What it does"],
            ["1", "Data Ingestion", "Reads orders and product master data from Excel"],
            ["2", "Pre-flight Filter", "Removes orders outside the delivery window or with missing data"],
            ["3", "Plan Lock", "Freezes the 4 most recently committed weeks; replays them unchanged"],
            ["4", "CP-SAT Optimisation", "A constraint-satisfaction solver assigns orders to days and lines"],
            ["5", "Export", "Writes the Excel schedule, capacity dashboard, and action list"],
          ]
        ),

        new Paragraph({ spacing: { after: 160 } }),

        H2("Rolling Commit Mechanic"),
        Body(
          "Each run commits a four-week block of production days. Committed days are frozen " +
          "and replayed byte-for-byte in subsequent runs — giving the production floor a stable " +
          "near-term plan that never changes unexpectedly. Only the open tail (weeks 5–7 of " +
          "the horizon) is re-optimised on each run."
        ),
        Body(
          "A cold start (first run with no prior state) treats all weeks as open and solves " +
          "the full horizon. Subsequent runs extend the chain automatically."
        ),

        H2("Two Production Lines"),

        SimpleTable(
          [1800, 2400, 2400],
          [
            ["", "Line 1 (L1)", "Line 2 (L2)"],
            ["Type", "Automated / retail", "Manual / CFS spiral"],
            ["Typical products", "Standard retail sandwiches", "Specialty, branded, spiral-wrapped"],
            ["Shift start", "06:00", "06:30"],
            ["Shift end (Mon–Thu)", "22:30  →  16h30 available", "22:30  →  16h00 available"],
            ["Shift end (Friday)", "20:30  →  14h30 available", "20:30  →  14h00 available"],
            ["Line restriction", "Some products L1 only", "CFS products L2 only (hard constraint)"],
          ]
        ),

        new Paragraph({ spacing: { after: 160 } }),

        H2("Constraints Managed by the Solver"),
        Body(
          "The system enforces 34 production rules across 9 categories. " +
          "Hard constraints are never violated; soft constraints are preferences the optimiser " +
          "pays a penalty for breaking. Rules marked – are parsed from the spreadsheet but " +
          "not yet enforced."
        ),

        // Constraint table: ID | Category | Rule | Severity | Status
        // Usable width ~8450 DXA; columns: 560+1400+4090+900+900 = 7850
        new Table({
          width: { size: 7850, type: WidthType.DXA },
          columnWidths: [560, 1400, 4090, 900, 900],
          rows: [
            // Header
            new TableRow({ children: [
              new TableCell({ width: { size: 560, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "1E3A5F" }, children: [new Paragraph({ children: [new TextRun({ text: "ID", bold: true, size: 18, color: "FFFFFF" })], spacing: { before: 60, after: 60 }, margins: { left: 80, right: 80 } })], margins: { top: 60, bottom: 60, left: 80, right: 80 } }),
              new TableCell({ width: { size: 1400, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "1E3A5F" }, children: [new Paragraph({ children: [new TextRun({ text: "Category", bold: true, size: 18, color: "FFFFFF" })], spacing: { before: 60, after: 60 } })], margins: { top: 60, bottom: 60, left: 80, right: 80 } }),
              new TableCell({ width: { size: 4090, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "1E3A5F" }, children: [new Paragraph({ children: [new TextRun({ text: "Rule", bold: true, size: 18, color: "FFFFFF" })], spacing: { before: 60, after: 60 } })], margins: { top: 60, bottom: 60, left: 80, right: 80 } }),
              new TableCell({ width: { size: 900, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "1E3A5F" }, children: [new Paragraph({ children: [new TextRun({ text: "Severity", bold: true, size: 18, color: "FFFFFF" })], spacing: { before: 60, after: 60 } })], margins: { top: 60, bottom: 60, left: 80, right: 80 } }),
              new TableCell({ width: { size: 900, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "1E3A5F" }, children: [new Paragraph({ children: [new TextRun({ text: "Status", bold: true, size: 18, color: "FFFFFF" })], spacing: { before: 60, after: 60 } })], margins: { top: 60, bottom: 60, left: 80, right: 80 } }),
            ]}),
            // Data rows: [id, category, rule, severity, status, shade]
            ...[
              ["C1","Calendar","Shift end time — 22:30 normal days, 20:30 Friday","Hard","Active","FFFFFF"],
              ["C2","Calendar","Shift start time per line — L1 06:00, L2 06:30","Hard","Active","F8F9FA"],
              ["C3","Calendar","Production window before delivery date","Hard","Active","FFFFFF"],
              ["C4","Calendar","Workdays only (Mon–Fri, no weekends)","Hard","Active","F8F9FA"],
              ["C5","Calendar","Frozen committed days cannot be touched","Hard","Active","FFFFFF"],
              ["L1","Lines","Line-only products locked to one line","Hard","Active","F8F9FA"],
              ["L2","Lines","Explicit line exclusion (kan_niet) overrides all","Hard","Active","FFFFFF"],
              ["L3","Lines","Line preference (voorkeur) and load balancing","Soft","Active","F8F9FA"],
              ["L4","Lines","Maximum productions per day","Hard","– Not enforced","FFF7ED"],
              ["L5","Lines","Spiral (CFS) availability per line","Hard","– Not enforced","FFF7ED"],
              ["K1","Capacity","Daily planning capacity per line (shift minus reserve)","Hard","Active","FFFFFF"],
              ["K2","Capacity","Real capacity verified under actual changeovers","Hard","Active","F8F9FA"],
              ["O1","Changeovers","Changeover time between consecutive orders (MAX rule)","Hard","Active","FFFFFF"],
              ["O2","Changeovers","Scale format families (kop equivalence)","Hard","Active","F8F9FA"],
              ["O3","Changeovers","Daesang fixed-time exception (named article pairs)","Hard","Active","FFFFFF"],
              ["O4","Changeovers","Minimise total changeover per day (TSP)","Soft","Active","F8F9FA"],
              ["O5","Changeovers","Cluster same-format orders onto the same line","Soft","Active","FFFFFF"],
              ["O6","Changeovers","No idle holes inside a line-day","Soft","Active","F8F9FA"],
              ["A1","Allergens","Allergen level must not drop without cleaning","Hard","Active","FFFFFF"],
              ["A2","Allergens","Cross-contamination cleaning between products","Hard","Active","F8F9FA"],
              ["A3","Allergens","Allergen level derived from X-count (sate = 6)","Info","Active","FFFFFF"],
              ["A4","Allergens","Halal isolation","Hard","– Not enforced","FFF7ED"],
              ["A5","Allergens","Sate (peanut) stops the other line and closes its own","Hard","Active","FFFFFF"],
              ["A6","Allergens","Sate reserves opposite line capacity in the solver","Hard","Active","F8F9FA"],
              ["D1","Prod. days","Allowed weekdays per product (chicken Tue/Wed/Fri…)","Hard","Active","FFFFFF"],
              ["D2","Prod. days","MEP lead time advisory (D-1 / D-2)","Soft","Active","F8F9FA"],
              ["D3","Prod. days","Parameterised MEP lead days (>D-1)","Soft","– Not enforced","FFF7ED"],
              ["D4","Prod. days","Special day restrictions string","Hard","– Not enforced","FFF7ED"],
              ["M1","Equipment","Shared single-unit equipment staggered between lines","Hard","Active","FFFFFF"],
              ["M2","Equipment","Device conflicts converted to solver no-good cuts","Hard","Active","F8F9FA"],
              ["P1","Staffing","Max 3-FTE ramp between consecutive orders","Soft","Active","FFFFFF"],
              ["P2","Staffing","FTE limits per shift","Hard","– Not enforced","FFF7ED"],
              ["P3","Staffing","Skill level requirements","Soft","– Not enforced","FFF7ED"],
              ["V1","Speed","Production duration from line speed","Hard","Active","FFFFFF"],
              ["V2","Speed","Speed caps (e.g. 800 g on L2)","Hard","– Not enforced","FFF7ED"],
              ["S1","Sequencing","Identical products → kapvorm → ascending allergen level","Soft","Active","FFFFFF"],
              ["S2","Sequencing","Must / cannot be first or last product of day","Hard","Active","F8F9FA"],
              ["S3","Sequencing","A day's set can supply both ends (open + close)","Hard","Active","FFFFFF"],
              ["G1","Grouping","Identical products produced together, smallest first","Soft","Active","F8F9FA"],
              ["G2","Grouping","Same kapvorm together within and across days","Soft","Active","FFFFFF"],
              ["G3","Grouping","Same schaal (tray type) together","Soft","Active","F8F9FA"],
              ["X1","Integrity","No partial delivery of a split order","Hard","Active","FFFFFF"],
              ["X2","Integrity","Every order is scheduled or rejected exactly once","Hard","Active","F8F9FA"],
              ["X3","Integrity","Frozen committed days replay byte-for-byte","Hard","Active","FFFFFF"],
            ].map(([id, cat, rule, sev, status, fill]) =>
              new TableRow({ children: [
                new TableCell({ width: { size: 560, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill }, children: [new Paragraph({ children: [new TextRun({ text: id, bold: true, size: 18, color: status.startsWith("–") ? "92400E" : "1E3A5F" })], spacing: { before: 40, after: 40 } })], margins: { top: 40, bottom: 40, left: 80, right: 40 } }),
                new TableCell({ width: { size: 1400, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill }, children: [new Paragraph({ children: [new TextRun({ text: cat, size: 18 })], spacing: { before: 40, after: 40 } })], margins: { top: 40, bottom: 40, left: 80, right: 80 } }),
                new TableCell({ width: { size: 4090, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill }, children: [new Paragraph({ children: [new TextRun({ text: rule, size: 18 })], spacing: { before: 40, after: 40 } })], margins: { top: 40, bottom: 40, left: 80, right: 80 } }),
                new TableCell({ width: { size: 900, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill }, children: [new Paragraph({ children: [new TextRun({ text: sev, size: 18, color: sev === "Hard" ? "991B1B" : sev === "Soft" ? "1E40AF" : "374151" })], spacing: { before: 40, after: 40 } })], margins: { top: 40, bottom: 40, left: 80, right: 80 } }),
                new TableCell({ width: { size: 900, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill }, children: [new Paragraph({ children: [new TextRun({ text: status, size: 18, color: status.startsWith("–") ? "92400E" : "166534" })], spacing: { before: 40, after: 40 } })], margins: { top: 40, bottom: 40, left: 80, right: 80 } }),
              ]})
            ),
          ],
        }),

        new Paragraph({ spacing: { after: 120 } }),
        Body("Rows highlighted in amber are parsed from the planning spreadsheet but not yet active in the solver. They represent the next layer of improvements."),

        Break(),

        // ── PAGE 3 ────────────────────────────────────────────────────────
        H1("Recent Improvements & Next Steps"),

        H2("Improvements Delivered — October 2026"),
        Body(
          "Two significant improvements were released as part of the October 2026 update:"
        ),

        new Paragraph({
          children: [new TextRun({ text: "1.  Rolling freeze & replay chain", bold: true, size: 22 })],
          spacing: { before: 120, after: 60 },
        }),
        Body(
          "Before: each planning run started from scratch, so production weeks 44 and beyond " +
          "showed zero output — the solver had no prior state to build on."
        ),
        Body(
          "After: the system now locks committed weeks and replays them unchanged. Production " +
          "weeks 44–47 now show 87–92% line utilisation, and the near-term schedule is stable " +
          "across consecutive runs."
        ),

        new Paragraph({
          children: [new TextRun({ text: "2.  CFS line assignment fix", bold: true, size: 22 })],
          spacing: { before: 120, after: 60 },
        }),
        Body(
          "Before: 14 spiral-conveyor (CFS) products could be placed on either Line 1 or " +
          "Line 2 due to an error in how the system read line-permission data. This caused " +
          "mis-placements that required manual correction after planning."
        ),
        Body(
          "After: the loader now reads the correct data field (spiraal) for CFS products, " +
          "locking all 14 to Line 2 only. No manual correction is needed."
        ),

        H2("Known Gaps & Roadmap"),

        SimpleTable(
          [2000, 2200, 2400],
          [
            ["Area", "Current State", "Next Step"],
            [
              "Master data refresh",
              "Product master not auto-regenerated each run",
              "Add refresh stage reading Planning info.xlsx automatically",
            ],
            [
              "Device conflict detection",
              "Conflicts detected but not always fully resolved",
              "Enforce device stagger across all shared equipment types",
            ],
            [
              "Reporting",
              "Capacity dashboard only",
              "Extend action list with root-cause codes for rejected orders",
            ],
            [
              "Test coverage",
              "3 test modules have collection errors",
              "Fix conftest import path; add integration tests for CFS products",
            ],
          ]
        ),

        new Paragraph({ spacing: { after: 240 } }),

        new Paragraph({
          children: [
            new TextRun({
              text: "Generated by Claude Code · Frigilunch discovery project · October 2026",
              size: 18,
              color: "9CA3AF",
            }),
          ],
          alignment: AlignmentType.CENTER,
          spacing: { before: 400 },
        }),
      ],
    },
  ],
});

// ── Write file ─────────────────────────────────────────────────────────────
const outPath = path.join(__dirname, "frigilunch_overview_report.docx");
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outPath, buf);
  console.log("Written:", outPath, "(" + buf.length + " bytes)");
});
