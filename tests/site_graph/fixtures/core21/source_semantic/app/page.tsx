import { CONTACT as SHARED_CONTACT, navItems } from "../src/navigation";
const ROOT = "/";
const contactCard = { href: SHARED_CONTACT, label: "Contact" };
export default function Home() {
  const runtimeTarget = getTargetFromRuntime();
  const stateTarget = props.currentTarget;
  return <main>
    <a href={ROOT}>Home</a>
    <WrappedLink to={routePath("resources")}>Resources</WrappedLink>
    <a href={runtimeTarget}>Runtime</a>
    <a href={process.env.PRIVATE_ROUTE}>Environment</a>
    <a href={stateTarget}>State</a>
    {navItems.filter((item) => item.visible).map((item) => <Link href={item.href}>{item.label}</Link>)}
    <Card {...contactCard} />
    <button onClick={() => router.push(SHARED_CONTACT)}>Contact</button>
  </main>;
}
