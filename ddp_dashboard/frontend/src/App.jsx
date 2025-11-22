import { useEffect, useState } from "react";
import { api } from "./api";

export default function App() {
  /* ------------ state ------------ */
  const [rows,    setRows]    = useState([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);

  const [videoId,     setVideoId]     = useState("");

  // after-date + optional time
  const [afterDate,   setAfterDate]   = useState("");   // YYYY-MM-DD
  const [afterTime,   setAfterTime]   = useState("");   // HH:MM
  // before-date + optional time
  const [beforeDate,  setBeforeDate]  = useState("");
  const [beforeTime,  setBeforeTime]  = useState("");

  /* ------------ fetch helper ------------ */
  const fetchData = async () => {
    setLoading(true);
    setError(null);

    const params = { limit: 100 };

    if (videoId.trim()) params.video_id = videoId.trim();

    if (afterDate) {
      const t = afterTime || "00:00";
      params.after = `${afterDate}T${t}`;
    }
    if (beforeDate) {
      const t = beforeTime || "23:59";
      params.before = `${beforeDate}T${t}`;
    }

    console.log("🔎 params →", params);

    try {
      const { data } = await api.get("/views", { params });
      setRows(data);
    } catch (err) {
      console.error(err);
      setError("Fetch failed – check console.");
    } finally {
      setLoading(false);
    }
  };

  /* ---------- initial load ---------- */
  useEffect(() => { fetchData(); }, []);

  /* ------------- UI -------------- */
  return (
    <div className="max-w-5xl mx-auto p-6">
      <h1 className="text-2xl font-semibold mb-4">TikTok View Explorer</h1>

      {/* Filters */}
      <div className="flex flex-wrap gap-6 items-end mb-6">

        {/* Video-ID */}
        <div>
          <label className="block text-sm">Video ID</label>
          <input
            type="text"
            value={videoId}
            onChange={e => setVideoId(e.target.value)}
            className="border rounded px-2 py-1 w-56"
            placeholder="e.g., 727889…"
          />
        </div>

        {/* After date/time */}
        <div>
          <label className="block text-sm">After – date</label>
          <input
            type="date"
            value={afterDate}
            onChange={e => setAfterDate(e.target.value)}
            className="border rounded px-2 py-1"
          />
        </div>
        <div>
          <label className="block text-sm">After – time&nbsp;<span className="text-xs">(optional)</span></label>
          <input
            type="time"
            value={afterTime}
            onChange={e => setAfterTime(e.target.value)}
            className="border rounded px-2 py-1"
          />
        </div>

        {/* Before date/time */}
        <div>
          <label className="block text-sm">Before – date</label>
          <input
            type="date"
            value={beforeDate}
            onChange={e => setBeforeDate(e.target.value)}
            className="border rounded px-2 py-1"
          />
        </div>
        <div>
          <label className="block text-sm">Before – time&nbsp;<span className="text-xs">(optional)</span></label>
          <input
            type="time"
            value={beforeTime}
            onChange={e => setBeforeTime(e.target.value)}
            className="border rounded px-2 py-1"
          />
        </div>

        <button
          onClick={fetchData}
          className="bg-blue-600 text-white px-4 py-2 rounded"
        >
          Apply
        </button>
      </div>

      {error && <p className="text-red-600 mb-4">{error}</p>}

      {/* Table */}
      {loading ? (
        <p>Loading…</p>
      ) : (
        <table className="w-full text-sm border divide-y bg-white">
          <thead className="bg-gray-50">
            <tr>
              <th className="p-2 text-left">Viewed at</th>
              <th className="p-2 text-left">Video ID</th>
              <th className="p-2 text-left">User ID</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, idx) => (
              <tr
                key={`${r.video_id}-${r.viewed_at}-${idx}`}   /* ← unique key */
                className="odd:bg-gray-50"
              >
                <td className="p-2">
                  {new Date(r.viewed_at).toLocaleString()}
                </td>
                <td className="p-2 font-mono">{r.video_id}</td>
                <td className="p-2">{r.user_id ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
