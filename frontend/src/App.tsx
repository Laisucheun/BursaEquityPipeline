import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Companies from "./pages/Companies";
import CompanyDetail from "./pages/CompanyDetail";
import Upload from "./pages/Upload";
import Review from "./pages/Review";
import Peers from "./pages/Peers";
import Feed from "./pages/Feed";
import ResultsReport from "./pages/ResultsReport";
import Brief from "./pages/Brief";

export default function App() {
  return (
    <>
      <nav className="topnav">
        <div className="wrap navrow">
          <span className="brand">Bursa Equity Pipeline</span>
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/feed">Results feed</NavLink>
          <NavLink to="/companies">Companies</NavLink>
          <NavLink to="/peers">Peers</NavLink>
          <NavLink to="/upload">Upload</NavLink>
          <NavLink to="/review">Concept review</NavLink>
        </div>
      </nav>
      <main className="wrap">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/feed" element={<Feed />} />
          <Route path="/feed/brief" element={<Brief />} />
          <Route path="/feed/brief/:date" element={<Brief />} />
          <Route path="/feed/:code" element={<ResultsReport />} />
          <Route path="/companies" element={<Companies />} />
          <Route path="/company/:code" element={<CompanyDetail />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/review" element={<Review />} />
          <Route path="/peers" element={<Peers />} />
          <Route path="*" element={<div className="panel empty">Page not found.</div>} />
        </Routes>
      </main>
    </>
  );
}
