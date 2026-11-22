// Minimal input helper for driving the VMware window with real key codes.
// Usage:
//   vmctl click X Y          left click at global screen point
//   vmctl dclick X Y         double click
//   vmctl rclick X Y         right click
//   vmctl drag X1 Y1 X2 Y2   left-button drag (splitters, scroll bars)
//   vmctl key NAME [ctrl] [shift] [alt]   e.g. key v ctrl ; key return ; key escape
//   vmctl type "ascii text"  types printable ASCII slowly (35 ms/char)
import CoreGraphics
import Foundation

let src = CGEventSource(stateID: .hidSystemState)

func mouse(_ type: CGEventType, _ p: CGPoint, _ button: CGMouseButton = .left, clicks: Int64 = 1) {
    let e = CGEvent(mouseEventSource: src, mouseType: type, mouseCursorPosition: p, mouseButton: button)!
    e.setIntegerValueField(.mouseEventClickState, value: clicks)
    e.post(tap: .cghidEventTap)
}

func click(_ p: CGPoint, double: Bool = false, right: Bool = false) {
    mouse(.mouseMoved, p)
    usleep(120_000)
    let down: CGEventType = right ? .rightMouseDown : .leftMouseDown
    let up: CGEventType = right ? .rightMouseUp : .leftMouseUp
    let btn: CGMouseButton = right ? .right : .left
    mouse(down, p, btn, clicks: 1); usleep(60_000); mouse(up, p, btn, clicks: 1)
    if double {
        usleep(90_000)
        mouse(down, p, btn, clicks: 2); usleep(60_000); mouse(up, p, btn, clicks: 2)
    }
}

// US ANSI virtual key codes.
let codes: [Character: (CGKeyCode, Bool)] = {
    var m: [Character: (CGKeyCode, Bool)] = [:]
    let letters: [(Character, CGKeyCode)] = [("a",0),("s",1),("d",2),("f",3),("h",4),("g",5),("z",6),("x",7),("c",8),("v",9),("b",11),("q",12),("w",13),("e",14),("r",15),("y",16),("t",17),("o",31),("u",32),("i",34),("p",35),("l",37),("j",38),("k",40),("n",45),("m",46)]
    for (c, k) in letters { m[c] = (k, false); m[Character(c.uppercased())] = (k, true) }
    let digits: [(Character, CGKeyCode, Character)] = [("1",18,"!"),("2",19,"@"),("3",20,"#"),("4",21,"$"),("5",23,"%"),("6",22,"^"),("7",26,"&"),("8",28,"*"),("9",25,"("),("0",29,")")]
    for (d, k, s) in digits { m[d] = (k, false); m[s] = (k, true) }
    let punct: [(Character, CGKeyCode, Character)] = [("-",27,"_"),("=",24,"+"),("[",33,"{"),("]",30,"}"),("\\",42,"|"),(";",41,":"),("'",39,"\""),(",",43,"<"),(".",47,">"),("/",44,"?"),("`",50,"~")]
    for (c, k, s) in punct { m[c] = (k, false); m[s] = (k, true) }
    m[" "] = (49, false)
    return m
}()

let named: [String: CGKeyCode] = ["return": 36, "tab": 48, "escape": 53, "delete": 51, "space": 49,
    "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119, "f5": 96]

let modKeys: [(CGEventFlags, CGKeyCode)] = [(.maskControl, 59), (.maskShift, 56), (.maskAlternate, 58)]

func raw(_ k: CGKeyCode, _ down: Bool, _ flags: CGEventFlags) {
    let e = CGEvent(keyboardEventSource: src, virtualKey: k, keyDown: down)!
    e.flags = flags; e.post(tap: .cghidEventTap)
    usleep(45_000)
}

// Real modifier presses: VMware ignores flags that are not backed by key events.
func key(_ k: CGKeyCode, _ flags: CGEventFlags) {
    var held: CGEventFlags = []
    var pressed: [CGKeyCode] = []
    for (f, code) in modKeys where flags.contains(f) {
        held.insert(f); raw(code, true, held); pressed.append(code)
    }
    raw(k, true, held); raw(k, false, held)
    for (f, code) in modKeys.reversed() where flags.contains(f) {
        held.remove(f); raw(code, false, held)
    }
    _ = pressed
}

func tapModifier(_ name: String) {
    let map: [String: (CGEventFlags, CGKeyCode)] = ["shift": (.maskShift, 56), "ctrl": (.maskControl, 59)]
    guard let (f, code) = map[name] else { exit(5) }
    raw(code, true, f); raw(code, false, [])
}

let a = CommandLine.arguments
guard a.count >= 2 else { exit(2) }
switch a[1] {
case "click", "dclick", "rclick":
    let p = CGPoint(x: Double(a[2])!, y: Double(a[3])!)
    click(p, double: a[1] == "dclick", right: a[1] == "rclick")
case "drag":
    let p1 = CGPoint(x: Double(a[2])!, y: Double(a[3])!), p2 = CGPoint(x: Double(a[4])!, y: Double(a[5])!)
    mouse(.mouseMoved, p1); usleep(150_000)
    mouse(.leftMouseDown, p1); usleep(150_000)
    for i in 1...20 {
        let t = Double(i) / 20.0
        mouse(.leftMouseDragged, CGPoint(x: p1.x + (p2.x - p1.x) * t, y: p1.y + (p2.y - p1.y) * t)); usleep(30_000)
    }
    usleep(150_000); mouse(.leftMouseUp, p2)
case "key":
    var flags: CGEventFlags = []
    for f in a.dropFirst(3) {
        if f == "ctrl" { flags.insert(.maskControl) }
        if f == "shift" { flags.insert(.maskShift) }
        if f == "alt" { flags.insert(.maskAlternate) }
    }
    let name = a[2].lowercased()
    if let k = named[name] { key(k, flags) }
    else if let c = name.first, let (k, _) = codes[c] { key(k, flags) }
    else { print("unknown key " + name); exit(3) }
case "tapmod":
    tapModifier(a[2])
case "type":
    for ch in a[2] {
        guard let (k, shift) = codes[ch] else { print("unsupported char"); exit(4) }
        key(k, shift ? .maskShift : [])
        usleep(35_000)
    }
default:
    exit(2)
}
