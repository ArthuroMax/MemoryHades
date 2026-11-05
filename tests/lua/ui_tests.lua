-- ===== tests for the Auto Farm / Farm Additional tabs (the real UI code, run against a recording stand-in for AdminUI) =====
local results = {}
local function check(name, cond, detail)
    results[#results + 1] = { name, cond and true or false }
    print((cond and "  PASS  " or "  FAIL  ") .. name .. ((not cond and detail) and ("   [" .. tostring(detail) .. "]") or ""))
end
local function build(caps)
    local tabs, saves = {}, { n = 0 }
    local S = { MovementMode = "Fly", AvoidAdaptive = true, MurdererSafeDist = 40, AvoidManual = 40, AutoJump = true, PostActions = "record",
                DieAtFullBag = false, KillAllOnFullBag = false, ShootMurdOnFullBag = false, FlingMurdOnFullBag = false, VisualIgnoreLimit = false,
                MemUrl = "https://memoryhades.onrender.com", MemToken = "" }
    local CoinFarm = { State = false, analysis = { rec = { SafeDist = 40 }, summary = "x" }, mem = { status = function() return "local only (0 spots)" end },
                       deriveDistances = function() end }
    local Extras, Threat, Legit = {}, {}, {}
    local MAX_SPEED = 26
    local function requestSave() saves.n += 1 end
    local function stopMovement() end
    local win = { Gui = {} }
    function win:AddTab(name)
        local tab = { name = name, n = 0, controls = {}, page = { GetChildren = function() return {} end } }
        local function add(kind)
            return function(self, o)
                self.n += 1
                local h = { kind = kind, opts = o, name = o.Name }
                h.Lock = function(reason) h.locked = reason end
                h.Unlock = function() h.locked = nil end
                self.controls[#self.controls + 1] = h
                return h
            end
        end
        for _, k in ipairs({ "AddToggle", "AddDropdown", "AddSlider", "AddLabel", "AddSegmented", "AddTextbox", "AddButton" }) do tab[k] = add(k) end
        if caps.paragraph then tab.AddParagraph = add("AddParagraph") end
        if caps.section then tab.AddSection = function(self, text) self.n += 1 self.controls[#self.controls + 1] = { kind = "section", name = text } end end
        tabs[#tabs + 1] = tab
        return tab
    end
    --UI_BLOCK--
    return tabs, S, Extras, saves
end
local function names(tab) local t = {} for _, c in ipairs(tab.controls) do if c.kind ~= "section" then t[#t + 1] = c.name end end return t end
local function find(tab, name) for _, c in ipairs(tab.controls) do if c.name == name then return c end end end
local function countName(tabs, name) local n = 0 for _, t in ipairs(tabs) do for _, c in ipairs(t.controls) do if c.name == name then n += 1 end end end return n end
local MOVED = { "Die at Full Bag", "Kill All at Full Bag", "Shoot Murderer at Full Bag", "Fling Murderer at Full Bag", "After bag full" }

local tabs, S, Extras, saves = build({ paragraph = true, section = true })
local farm, extra = tabs[1], tabs[2]
print("== U1. tab layout ==")
check("U1.1 tabs are created in the order Auto Farm, Farm Additional", farm.name == "Auto Farm" and extra and extra.name == "Farm Additional" and #tabs == 2)
local leaked = {}
for _, n in ipairs(MOVED) do if find(farm, n) then leaked[#leaked + 1] = n end end
check("U1.2 Auto Farm no longer contains any of the five relocated controls", #leaked == 0, table.concat(leaked, ","))
local exn = names(extra)
check("U1.3 Farm Additional holds exactly the five controls, in the original order", table.concat(exn, "|") == table.concat(MOVED, "|"), table.concat(exn, "|"))
local dup = false
for _, n in ipairs(MOVED) do if countName(tabs, n) ~= 1 then dup = true end end
check("U1.4 no duplicate controls anywhere", not dup)
local keep = { "Auto Farm", "Farm Mode", "Auto Avoid Distance", "Murderer Avoid Distance", "Recommended Avoid", "Jump Over Obstacles", "Memory server URL", "Memory token", "Navigation memory", "Visual Ignore Limit", "Murderer", "Distance", "Map", "Layout", "Fly Speed" }
local missing = {}
for _, n in ipairs(keep) do if countName(tabs, n) ~= 1 or not find(farm, n) then missing[#missing + 1] = n end end
check("U1.5 every other Auto Farm control is still there exactly once", #missing == 0, table.concat(missing, ","))

print("\n== U2. callbacks and saved settings still reference the right keys ==")
local die, kill, shoot, fling, after = find(extra, MOVED[1]), find(extra, MOVED[2]), find(extra, MOVED[3]), find(extra, MOVED[4]), find(extra, MOVED[5])
local n0 = saves.n
die.opts.Set(true) kill.opts.Set(true) shoot.opts.Set(true) fling.opts.Set(true)
check("U2.1 each toggle writes its own settings key and requests a save", S.DieAtFullBag == true and S.KillAllOnFullBag == true and S.ShootMurdOnFullBag == true and S.FlingMurdOnFullBag == true and saves.n == n0 + 4)
S.DieAtFullBag, S.KillAllOnFullBag, S.ShootMurdOnFullBag, S.FlingMurdOnFullBag = false, true, false, true
check("U2.2 Get reads the saved values (restored state is shown correctly)", die.opts.Get() == false and kill.opts.Get() == true and shoot.opts.Get() == false and fling.opts.Get() == true)
check("U2.3 'After bag full' is the multi-select chip row with the same options", after.kind == "AddSegmented" and after.opts.Multi == true and table.concat(after.opts.Options, ",") == "record,rest,diagnostics")
after.opts.Set({ "record", "rest" })
check("U2.4 its callback stores the comma list and saves; Get returns it", S.PostActions == "record,rest" and after.opts.Get() == "record,rest")

print("\n== U3. safety locks stay in place ==")
check("U3.1 the three attack toggles are still locked ('disabled')", kill.locked == "disabled" and shoot.locked == "disabled" and fling.locked == "disabled")
check("U3.2 Die at Full Bag and After bag full are NOT locked", die.locked == nil and after.locked == nil)
check("U3.3 Extras.atk still lists exactly those three handles", #Extras.atk == 3 and Extras.atk[1] == kill and Extras.atk[2] == shoot and Extras.atk[3] == fling)
check("U3.4 no unexpected rows in either tab", #farm.controls == #keep and #extra.controls == 6)

print("\n== U4. older AdminUI library (no AddParagraph / AddSection): degrades like before, no error ==")
local okb, tabs2 = pcall(build, { paragraph = false, section = false })
local t2 = okb and tabs2 or {}
check("U4.1 builds without errors", okb, tabs2)
if okb then
    local f2, e2 = t2[1], t2[2]
    check("U4.2 Farm Additional keeps the four toggles; the chip row and memory text boxes are skipped as before", #names(e2) == 4 and not find(e2, "After bag full") and not find(f2, "Memory token"))
end

local pass = 0
for _, r in ipairs(results) do if r[2] then pass += 1 end end
print(("\nlua ui summary: %d / %d passed"):format(pass, #results))
if pass ~= #results then error("failures") end
